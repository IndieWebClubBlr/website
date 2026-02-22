# Webring

The IndieWebClub Bangalore [Webring](https://indieweb.org/webring) is a way to connect the websites of its member and encourage discovery within our community. If you're a member, please consider adding the webring navigation to your website.

## Adding the Webring to Your Site

The webring provides links to two randomly-selected member websites: _Previous_ and _Next_. These links change every day, helping people discover each member's work.

The webring files are hosted on our website:

- `https://blr.indiewebclub.org/webring/previous.html`
- `https://blr.indiewebclub.org/webring/next.html`

They redirect visitors to the chosen members' websites.

Add the following HTML to your website (e.g., in the footer or sidebar), or add a custom one that uses the URLs above:

```html
<div class="webring">
  <a href="https://blr.indiewebclub.org/webring/previous.html">← Previous</a>
  | <a href="https://blr.indiewebclub.org/">IndieWebClub Bangalore</a> |
  <a href="https://blr.indiewebclub.org/webring/next.html">Next →</a>
</div>
```

You can customize the appearance with CSS:

```css
.webring {
  border: 1px solid #ccc;
  border-radius: 4px;
  text-align: center;
  font-size: 0.9rem;
  padding: 0.5rem;
}

.webring a {
  text-decoration: none;
  font-weight: bold;
}

.webring a:hover {
  text-decoration: underline;
}
```

It will look something like this:

<div class="webring">
  <a href="https://blr.indiewebclub.org/webring/previous.html">← Previous</a>
  | <a href="https://blr.indiewebclub.org/">IndieWebClub Bangalore</a> |
  <a href="https://blr.indiewebclub.org/webring/next.html">Next →</a>
</div>

## Requirements

First, you must participate in the IndieWebClub Bangalore [meetups](/#upcoming-events) and join our online community. Then, you must add your blog's feed to the [`blogroll.opml`](/blogroll.opml) file with a `htmlUrl` pointing to your website. Also, your feed must contain at least one post.

Please [raise a pull request](https://github.com/IndieWebClubBlr/website/pulls) to add your blog, or contact an admin in the community space.

## Disclaimer

The webring connects member websites but does not endorse their content. Members are responsible for their own sites. If you have concerns about a member's website, please contact the admins via [email](mailto:blr+mod@indiewebclub.org).
