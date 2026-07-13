AGENT_LABEL := com.dsorin2035.youtube2podcast-sync
AGENT_PLIST := $(HOME)/Library/LaunchAgents/$(AGENT_LABEL).plist
REPO_DIR := $(CURDIR)

.venv:
	uv sync

setup: .venv

sync: .venv
	git checkout main
	git pull origin main
	uv run sync.py

install-agent:
	mkdir -p $(REPO_DIR)/logs
	sed -e 's#{{REPO_DIR}}#$(REPO_DIR)#g' -e 's#{{HOME}}#$(HOME)#g' \
		launchd/$(AGENT_LABEL).plist.template > $(AGENT_PLIST)
	launchctl unload $(AGENT_PLIST) 2>/dev/null || true
	launchctl load $(AGENT_PLIST)
	@echo "Installed and loaded $(AGENT_PLIST)"

uninstall-agent:
	launchctl unload $(AGENT_PLIST) 2>/dev/null || true
	rm -f $(AGENT_PLIST)
	@echo "Uninstalled $(AGENT_PLIST)"

.PHONY: setup sync install-agent uninstall-agent
