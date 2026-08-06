.PHONY: all test build lint clean protocol-check linuxenv-test wx-safety-test app-dir-test

all: test build

# 1. Run protocol, rootfs-installer, W^X permission-safety and APP_DIR
#    alignment (Kotlin host <-> Python runtime) regression checks.
test: protocol-check linuxenv-test wx-safety-test app-dir-test
protocol-check:
	@echo "--- Running ZMUX RFC-6455 Protocol Conformance Check ---"
	@python3 experimental/zmux-kotlin/tools/mock_pty_ws_server.py --host 127.0.0.1 --port 8011 --token dev & \
	MOCK_PID=$$!; \
	sleep 1; \
	python3 experimental/zmux-kotlin/tools/protocol_check.py --port 8011 --token dev; \
	kill $$MOCK_PID || true

linuxenv-test:
	@echo "--- Running Linux rootfs installer regression tests ---"
	@PYTHONDONTWRITEBYTECODE=1 python3 experimental/zmux-kotlin/tests/test_linuxenv.py

wx-safety-test: linuxenv-test
	@echo "--- Running W^X / 'Permission denied' safety gates ---"
	@PYTHONDONTWRITEBYTECODE=1 python3 experimental/zmux-kotlin/tests/test_wx_permission_safety.py

app-dir-test: wx-safety-test
	@echo "--- Running APP_DIR alignment gates (Chaquopy AssetFinder / rootfs location) ---"
	@PYTHONDONTWRITEBYTECODE=1 python3 experimental/zmux-kotlin/tests/test_app_dir_alignment.py

# 2. Build Android Debug APK (requires JDK 17 & Gradle/Android SDK)
build:
	@echo "--- Building ZMUX Kotlin Native UI APK ---"
	@cd experimental/zmux-kotlin && \
	if [ ! -f ./gradlew ]; then gradle wrapper --gradle-version 8.7; fi && \
	chmod +x ./gradlew && \
	./gradlew :app:assembleDebug

# 3. Run Android Lint
lint:
	@echo "--- Running Android Lint ---"
	@cd experimental/zmux-kotlin && \
	if [ ! -f ./gradlew ]; then gradle wrapper --gradle-version 8.7; fi && \
	chmod +x ./gradlew && \
	./gradlew :app:lintDebug

# 4. Clean build outputs
clean:
	@cd experimental/zmux-kotlin && \
	if [ -f ./gradlew ]; then ./gradlew clean; fi
