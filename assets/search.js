// Full-text search for /search/. Loads the tinysearch wasm engine (hashed
// filename baked into the page, so browser caches keep it across builds) on
// first interaction and runs queries entirely client-side. The current query
// is kept in the URL (?q=...), so searches can be linked and the back button
// steps through previous queries.
//
// The engine is fetched as a pre-compressed .gz sibling and decompressed in
// the browser, so the download is ~1 MB instead of ~1.8 MB on any host
// (GitHub Pages would also serve the .gz transparently, but that behaviour
// is not documented). If the compressed file is missing or already decoded
// by the server, the plain .wasm is used instead.
//
// The script is loaded asynchronously and starts as soon as the DOM is
// ready: deferred scripts run in document order, so waiting for the
// third-party analytics script in the page head would delay the index.
(function () {
  "use strict";

  // Normalize British spellings to their American counterparts before
  // stemming. The map is inlined into the page by the generator, so the
  // browser and the build index use exactly the same data.
  function normalizeSpelling(query) {
    const map = window.SPELLING_MAP;
    if (!map) return query;
    return query
      .replace(/[^\p{L}']+/gu, " ")
      .split(/\s+/)
      .filter(function (token) {
        return token.length > 0;
      })
      .map(function (token) {
        const key = token.toLowerCase();
        return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : token;
      })
      .join(" ");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initSearch);
  } else {
    initSearch();
  }

  function initSearch() {
    const container = document.getElementById("search-page");
    if (!container) return;

    const wasmFile = container.getAttribute("data-wasm");
    const wasmGzFile = wasmFile + ".gz";
    const ref = container.getAttribute("data-ref") || "";
    const input = document.getElementById("search-input");
    const statusEl = document.getElementById("search-status");
    const resultsEl = document.getElementById("search-results");

    const MAX_RESULTS = 30;
    const MIN_QUERY_LENGTH = 3;
    const DEBOUNCE_MS = 300;
    const QUERY_PARAM = "q";
    // The engine exports no allocator, and its own allocator takes the
    // memory from the end and grows upwards. So the query string is parked
    // in a page we reserve below that arena and reused for every query:
    // the memory does not grow with the number of queries.
    const QUERY_PAGE = 65536;

    let engine = null;
    let enginePromise = null;
    let loadFailed = false;
    let debounceTimer = null;
    let queryPtr = 0;

    function readQueryFromUrl() {
      const params = new URLSearchParams(window.location.search);
      return (params.get(QUERY_PARAM) || "").trim();
    }

    function syncUrlQuery(query) {
      const url = new URL(window.location.href);
      const current = url.searchParams.get(QUERY_PARAM) || "";
      if (
        current === query &&
        (query !== "" || !url.searchParams.has(QUERY_PARAM))
      ) {
        return;
      }
      if (query) {
        url.searchParams.set(QUERY_PARAM, query);
      } else {
        url.searchParams.delete(QUERY_PARAM);
      }
      window.history.pushState(null, "", url.toString());
    }

    const initialQuery = readQueryFromUrl();
    if (initialQuery) input.value = initialQuery;

    // The box stays disabled until the engine is ready: querying before it
    // loaded would silently do nothing.
    input.disabled = true;
    input.placeholder = "Loading search index…";

    function setStatus(message, isError) {
      statusEl.textContent = message;
      if (isError) {
        statusEl.classList.add("search-error");
      } else {
        statusEl.classList.remove("search-error");
      }
    }

    function allocString(exports, length) {
      if (exports.malloc) {
        return exports.malloc(length);
      }
      if (!queryPtr) {
        exports.memory.grow(1);
        queryPtr = exports.memory.buffer.byteLength - QUERY_PAGE;
      }
      return queryPtr;
    }

    function stringToWasm(exports, str) {
      let bytes = new TextEncoder().encode(str + "\0");
      if (bytes.length > QUERY_PAGE) {
        // A query this long is absurd; cut it to the reserved page.
        bytes = bytes.slice(0, QUERY_PAGE - 1);
        bytes[bytes.length - 1] = 0;
      }
      const ptr = allocString(exports, bytes.length);
      new Uint8Array(exports.memory.buffer, ptr, bytes.length).set(bytes);
      return ptr;
    }

    function wasmToString(exports, ptr) {
      if (!ptr) return null;
      const mem = new Uint8Array(exports.memory.buffer);
      let end = ptr;
      while (end < mem.length && mem[end] !== 0) end++;
      return new TextDecoder().decode(mem.subarray(ptr, end));
    }

    function withRef(url) {
      if (!ref) return url;
      let hash = "";
      let base = url;
      const hashIndex = url.indexOf("#");
      if (hashIndex >= 0) {
        hash = url.slice(hashIndex);
        base = url.slice(0, hashIndex);
      }
      const qIndex = base.indexOf("?");
      const path = qIndex >= 0 ? base.slice(0, qIndex) : base;
      const query = qIndex >= 0 ? base.slice(qIndex + 1) : "";
      const kept = query.split("&").filter(function (pair) {
        return pair && pair.indexOf("ref=") !== 0;
      });
      kept.push("ref=" + encodeURIComponent(ref));
      return path + "?" + kept.join("&") + hash;
    }

    function isWasm(bytes) {
      // A wasm module starts with the magic bytes \0asm.
      if (!bytes || bytes.byteLength < 4) return false;
      const head = new Uint8Array(bytes, 0, 4);
      return (
        head[0] === 0x00 &&
        head[1] === 0x61 &&
        head[2] === 0x73 &&
        head[3] === 0x6d
      );
    }

    function gunzip(bytes) {
      // Decompress gzip bytes in the browser. If the server already decoded
      // the file (it sent Content-Encoding: gzip), the bytes are the plain
      // engine and decompressing fails — keep them as they are.
      if (typeof DecompressionStream === "undefined") {
        return Promise.resolve(bytes);
      }
      const stream = new Blob([bytes])
        .stream()
        .pipeThrough(new DecompressionStream("gzip"));
      return new Response(stream)
        .arrayBuffer()
        .then(function (decompressed) {
          return decompressed.byteLength ? decompressed : bytes;
        })
        .catch(function () {
          return bytes;
        });
    }

    function fetchEngineBytes() {
      // Prefer the pre-compressed sibling; fall back to the plain engine
      // when it is missing, cannot be decoded (no DecompressionStream, or
      // the server already decoded it), or is not a wasm module at all.
      return fetch(wasmGzFile)
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.arrayBuffer();
        })
        .then(gunzip)
        .then(function (bytes) {
          if (isWasm(bytes)) return bytes;
          throw new Error("not a wasm module");
        })
        .catch(function () {
          return fetch(wasmFile).then(function (response) {
            if (!response.ok) throw new Error("HTTP " + response.status);
            return response.arrayBuffer();
          });
        });
    }

    function loadEngine() {
      if (loadFailed) {
        // The engine already failed to load; do not retry on every keystroke.
        return Promise.reject(new Error("search engine unavailable"));
      }
      if (!enginePromise) {
        setStatus("Preparing search…");
        enginePromise = fetchEngineBytes()
          .then(function (bytes) {
            return WebAssembly.instantiate(bytes, {});
          })
          .then(function (result) {
            const exports = result.instance.exports;
            engine = {
              search: function (query, count) {
                const queryStringPtr = stringToWasm(exports, query);
                const resultPtr = exports.search(queryStringPtr, count);
                if (!resultPtr) return [];
                const json = wasmToString(exports, resultPtr);
                exports.free_search_result(resultPtr);
                try {
                  return JSON.parse(json);
                } catch (e) {
                  return [];
                }
              },
            };
            setStatus("Search ready. Type at least 3 characters to search.");
            input.disabled = false;
            input.placeholder = "Search posts…";
            input.focus();
            return engine;
          })
          .catch(function (e) {
            enginePromise = null;
            loadFailed = true;
            input.placeholder = "Search is unavailable";
            setStatus(
              "Search could not be loaded. You can still browse the archive.",
              true
            );
            throw e;
          });
      }
      return enginePromise;
    }

    function renderResults(query, results) {
      resultsEl.textContent = "";
      if (!results.length) {
        setStatus("No results for “" + query + "”.");
        return;
      }
      const suffix =
        results.length >= MAX_RESULTS ? " (top " + MAX_RESULTS + ")" : "";
      setStatus(
        results.length + (results.length === 1 ? " result" : " results") + suffix + "."
      );
      const list = document.createElement("ul");
      list.className = "search-results";
      results.forEach(function (result) {
        const item = document.createElement("li");
        item.className = "search-result";
        const link = document.createElement("a");
        link.className = "search-result-title";
        link.textContent = result.title || result.url;
        // The URL comes from a member's feed: only web links become
        // clickable, anything else (a javascript: URL, say) stays text.
        if (/^https?:\/\//i.test(result.url)) {
          link.href = withRef(result.url);
        }
        item.appendChild(link);
        if (result.meta) {
          const meta = document.createElement("div");
          meta.className = "search-result-meta";
          meta.textContent = result.meta;
          item.appendChild(meta);
        }
        list.appendChild(item);
      });
      resultsEl.appendChild(list);
    }

    function runSearch(options) {
      const updateUrl = !options || options.updateUrl !== false;
      const query = input.value.trim();
      if (updateUrl) {
        syncUrlQuery(query.length >= MIN_QUERY_LENGTH ? query : "");
      }
      if (query.length < MIN_QUERY_LENGTH) {
        resultsEl.textContent = "";
        setStatus("Type at least 3 characters to search");
        return;
      }
      loadEngine()
        .then(function (active) {
          const normalizedQuery = normalizeSpelling(query);
          const engineQuery =
            typeof stemQuery === "function"
              ? stemQuery(normalizedQuery)
              : normalizedQuery;
          renderResults(query, active.search(engineQuery, MAX_RESULTS));
        })
        .catch(function () {
          /* status already set by loadEngine */
        });
    }

    function cancelPendingSearch() {
      if (debounceTimer) {
        window.clearTimeout(debounceTimer);
        debounceTimer = null;
      }
    }

    function onInput() {
      cancelPendingSearch();
      debounceTimer = window.setTimeout(function () {
        debounceTimer = null;
        runSearch();
      }, DEBOUNCE_MS);
    }

    function onKeyDown(event) {
      if (event.key !== "Enter" || event.isComposing) return;
      event.preventDefault();
      cancelPendingSearch();
      runSearch();
    }

    input.addEventListener("focus", function () {
      loadEngine().catch(function () {});
    });
    input.addEventListener("input", onInput);
    input.addEventListener("keydown", onKeyDown);

    window.addEventListener("popstate", function () {
      cancelPendingSearch();
      input.value = readQueryFromUrl();
      runSearch({ updateUrl: false });
    });

    // Start downloading the index as soon as the page opens: autofocus does
    // not fire a focus event, so waiting for one would leave the engine
    // unloaded until the user clicked out of and back into the box.
    loadEngine().catch(function () {});
    if (initialQuery) runSearch({ updateUrl: false });
  }
})();
