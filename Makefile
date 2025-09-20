# OPML Feed Aggregator Makefile
# Provides convenient commands for development and usage

.PHONY: help setup install clean run

# Default Python command
PYTHON := python3
VENV_DIR := venv
VENV_PYTHON := $(VENV_DIR)/bin/python
VENV_PIP := $(VENV_DIR)/bin/pip

# Default target
help:
	@echo "OPML Feed Aggregator"
	@echo "===================="
	@echo ""
	@echo "Available commands:"
	@echo "  make setup     - Set up virtual environment and install dependencies"
	@echo "  make install   - Install dependencies (assumes venv exists)"
	@echo "  make run       - Run with sample OPML file"
	@echo "  make clean     - Remove virtual environment and generated files"
	@echo "  make help      - Show this help message"

# Set up virtual environment and install dependencies
setup: $(VENV_DIR)/bin/activate clean
	@echo "Setup complete! Virtual environment ready."
	@echo "To activate: source $(VENV_DIR)/bin/activate"

$(VENV_DIR)/bin/activate: requirements.txt
	@echo "Creating virtual environment..."
	$(PYTHON) -m venv $(VENV_DIR)
	@echo "Installing dependencies..."
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -r requirements.txt
	@touch $(VENV_DIR)/bin/activate

# Install dependencies (assumes virtual environment exists)
install:
	@if [ ! -d "$(VENV_DIR)" ]; then \
		echo "Virtual environment not found. Run 'make setup' first."; \
		exit 1; \
	fi
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -r requirements.txt

# Run the script with sample OPML
run: setup iwcb.opml
	@echo "Running OPML Feed Aggregator with sample data..."
	mkdir -p _site/
	$(VENV_PYTHON) generator.py iwcb.opml _site/index.html
	@echo "Generated output.html"

# Clean up generated files and virtual environment
clean:
	@echo "Cleaning up..."
	rm -f _site/index.html
	rm -f *.pyc
	rm -rf __pycache__
	@echo "Cleanup complete"

clean_venv:
	@echo "Cleaning up virtual environment..."
	rm -rf $(VENV_DIR)

clean_all: clean clean_venv

serve:
	python3 -m http.server -d ./_site/
