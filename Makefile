.PHONY: app tray-app clean ensure-venv ensure-tray-venv

VENV := .venv
PYTHON := $(VENV)/bin/python
PYINSTALLER := $(VENV)/bin/pyinstaller
PYINSTALLER_CONFIG_DIR := $(CURDIR)/build/.pyinstaller

app: ensure-venv
	@mkdir -p "$(PYINSTALLER_CONFIG_DIR)"
	PYINSTALLER_CONFIG_DIR="$(PYINSTALLER_CONFIG_DIR)" \
		$(PYINSTALLER) --noconfirm --clean packaging/hitl_queue.spec

tray-app: ensure-tray-venv
	@mkdir -p "$(PYINSTALLER_CONFIG_DIR)"
	PYINSTALLER_CONFIG_DIR="$(PYINSTALLER_CONFIG_DIR)" \
		$(PYINSTALLER) --noconfirm --clean packaging/hitl_tray.spec

ensure-venv:
	@if [ ! -x "$(PYTHON)" ]; then \
		python3 -m venv "$(VENV)"; \
	fi
	@if ! "$(PYTHON)" -c 'import PyInstaller, webview, hitl_queue' >/dev/null 2>&1; then \
		"$(PYTHON)" -m pip install -e '.[app]' pyinstaller; \
	fi

ensure-tray-venv:
	@if [ ! -x "$(PYTHON)" ]; then \
		python3 -m venv "$(VENV)"; \
	fi
	@if ! "$(PYTHON)" -c 'import PyInstaller, pystray, PIL, hitl_queue' >/dev/null 2>&1; then \
		"$(PYTHON)" -m pip install -e '.[tray]' pyinstaller; \
	fi

clean:
	rm -rf build dist
