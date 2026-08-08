# ZCode Mirror — convenience targets.
# Collector commands run on the internet-facing machine; intranet commands on
# the air-gapped host. Run `make help` for the list.

BUILD_ROOT ?= build
DIST_DIR   ?= dist
PKG        ?= $(wildcard $(DIST_DIR)/*.zdoc)
PY         ?= python3

.PHONY: help deps collect collect-fast dev-server import up down logs reindex clean

help: ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

# ---- 外网 (collector) ----
deps: ## install collector python deps
	$(PY) -m pip install -r collector/requirements.txt

collect: ## build a full .zdoc package (with Windows installers)
	$(PY) -m collector.export --build-root $(BUILD_ROOT) --out $(DIST_DIR)

collect-fast: ## build a .zdoc WITHOUT the ~200MB Windows installers
	$(PY) -m collector.export --build-root $(BUILD_ROOT) --out $(DIST_DIR) --skip-releases

dev-server: ## serve build/site locally on :8765 for preview
	@echo "open http://localhost:8765/cn/index.html"
	$(PY) -m http.server 8765 --directory $(BUILD_ROOT)/site

# ---- 内网 (intranet) ----
import: ## import/upgrade the latest .zdoc into intranet/data
	@if [ -z "$(PKG)" ]; then echo "没有找到 .zdoc，先在能联网的机器上运行 make collect"; exit 1; fi
	cd intranet && ./import.sh ../$(PKG)

up: ## docker compose up -d --build (run after import)
	cd intranet && docker compose up -d --build

down: ## docker compose down
	cd intranet && docker compose down

logs: ## tail container logs
	cd intranet && docker compose logs -f

reindex: ## rebuild the AI retrieval index after a manual data swap
	cd intranet && docker compose exec -T ai curl -sf -X POST http://localhost:8000/api/reindex

clean: ## remove build artefacts (keeps dist/*.zdoc)
	rm -rf $(BUILD_ROOT)
