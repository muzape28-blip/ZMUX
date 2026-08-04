.PHONY: all test build lint clean protocol-check

all: test build

# 1. Run Python RFC-6455 protocol verification (9/9 gates)
test: protocol-check
protocol-check:
	@echo "--- Running ZMUX RFC-6455 Protocol Conformance Check ---"
	@python3 experimental/zmux-kotlin/tools/mock_pty_ws_server.py --host 127.0.0.1 --port 8011 --token dev & \
	MOCK_PID=$$!; \
	sleep 1; \
	python3 experimental/zmux-kotlin/tools/protocol_check.py --port 8011 --token dev; \
	kill $$MOCK_PID || true

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
