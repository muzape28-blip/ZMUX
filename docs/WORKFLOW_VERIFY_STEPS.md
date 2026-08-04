# Workflow hardening — pending patch (needs `workflows` permission)

**Status:** NOT yet applied to `.github/workflows/build-zmux-apk.yml`. The
sandbox GitHub App used to push this branch lacks the `workflows` permission,
so the workflow file could not be updated from here. The equivalent
protections that **are** in the app itself:

- `gates` now reads the shipped `libproot.so` `DT_NEEDED` on the phone
  (`app/zmux/elfscan.py`) and says "STALE BINARY" when it still needs
  `libtalloc.so.2`.
- `zmux-info` prints `Proot NEEDED: …` and flags `[STALE — reinstall]`.
- The build script already fails CI if any `DT_NEEDED` cannot be satisfied
  by the packaged files (`verify_needed` in `scripts/build_proot_android.py`).

**To apply this patch:** grant the GitHub App `workflows: write` in
`Settings → GitHub Apps → Arena` (or push it yourself from a token with that
permission), then:

```bash
cd ZABAWHEELS
git apply docs/WORKFLOW_VERIFY_STEPS.patch
git add .github/workflows/build-zmux-apk.yml
git commit -m "CI: verify APK contents, bust proot cache, record build marker"
git push
```

(If you edit the file by hand instead of applying the patch: three changes —
(1) cache `key` gains `'scripts/build_proot_android.py'` in its hashFiles,
(2) a "Record build marker" step writes `build_marker.txt` before buildozer,
(3) a "Verify APK contents" step after the build unzips the APK and fails on
stale `libtalloc.so.2` NEEDED entries.)

```diff
diff --git a/.github/workflows/build-zmux-apk.yml b/.github/workflows/build-zmux-apk.yml
index 280169b..73d0493 100644
--- a/.github/workflows/build-zmux-apk.yml
+++ b/.github/workflows/build-zmux-apk.yml
@@ -93,7 +93,11 @@ jobs:
             app/.buildozer
             ~/.gradle/caches
             ~/.gradle/wrapper
-          key: ${{ runner.os }}-zmux-${{ hashFiles('app/buildozer.spec', 'toolchain/runtime-lock.json') }}
+          # The cached app/.buildozer holds the p4a dist, which embeds the
+          # proot/talloc .so files at dist-build time. Any change to the
+          # proot build script (or spec/lock) MUST invalidate it, otherwise a
+          # "fixed" build silently re-packages stale binaries.
+          key: ${{ runner.os }}-zmux-${{ hashFiles('app/buildozer.spec', 'toolchain/runtime-lock.json', 'scripts/build_proot_android.py') }}
           restore-keys: ${{ runner.os }}-zmux-
 
       - name: Ensure Android NDK r28c (shared by the PRoot build and buildozer)
@@ -118,11 +122,68 @@ jobs:
             --out libs \
             --abis armeabi-v7a,arm64-v8a
 
+      - name: Record build marker (on-device identification)
+        # build_marker.txt is packaged into the app (private.tar) and read by
+        # zmux.buildinfo; `zmux-info` and `gates` print it so the exact build
+        # on a phone is provable (stale-APK reports keep confusing "which
+        # build is installed").
+        run: |
+          printf '%s run=%s\n' "$(git rev-parse HEAD)" "$GITHUB_RUN_ID" > build_marker.txt
+          cat build_marker.txt
+
       - name: Build signed-by-debug-key universal APK
         env:
           SOURCE_DATE_EPOCH: "1785283200"
         run: buildozer -v android debug
 
+      - name: Verify APK contents (proot/talloc fix must be present)
+        # Unzip the final APK and prove the fix actually made it in: the
+        # packaged libproot.so must need "libtalloc.so" (never
+        # "libtalloc.so.2"), libtalloc.so must be shipped next to it, and the
+        # runtime self-heal + build marker must be inside private.tar. A
+        # stale/cached dist can otherwise produce a green build with a broken
+        # APK, and this step fails the workflow instead of the user's phone.
+        run: |
+          set -euo pipefail
+          NDK_BIN="$HOME/.buildozer/android/platform/android-ndk-r28c/toolchains/llvm/prebuilt/linux-x86_64/bin"
+          APK=$(find bin -maxdepth 1 -name '*.apk' -type f -print -quit)
+          test -n "$APK"
+          rm -rf /tmp/apk-verify && mkdir -p /tmp/apk-verify
+          unzip -q "$APK" -d /tmp/apk-verify
+          FAIL=0
+          for ABI in armeabi-v7a arm64-v8a; do
+            PROOT="/tmp/apk-verify/lib/$ABI/libproot.so"
+            TALLOC="/tmp/apk-verify/lib/$ABI/libtalloc.so"
+            echo "== $ABI =="
+            test -f "$PROOT"  || { echo "FAIL: $PROOT missing"; FAIL=1; }
+            test -f "$TALLOC" || { echo "FAIL: $TALLOC missing"; FAIL=1; }
+            NEEDED=$("$NDK_BIN/llvm-readelf" -d "$PROOT" 2>/dev/null | grep "Shared library" || true)
+            echo "$NEEDED"
+            if echo "$NEEDED" | grep -q "libtalloc.so.2"; then
+              echo "FAIL: $PROOT still needs libtalloc.so.2"; FAIL=1
+            fi
+            echo "$NEEDED" | grep -q "libtalloc.so" || { echo "FAIL: $PROOT does not need libtalloc.so"; FAIL=1; }
+            SONAME=$("$NDK_BIN/llvm-readelf" -d "$TALLOC" 2>/dev/null | grep "SONAME" || true)
+            echo "$SONAME"
+            if echo "$SONAME" | grep -q "libtalloc.so.2"; then
+              echo "FAIL: $TALLOC still claims SONAME libtalloc.so.2"; FAIL=1
+            fi
+          done
+          # private.tar is gzip-compressed despite its name; the runtime
+          # self-heal constant and the build marker must be inside it.
+          if ! (unzip -p "$APK" assets/private.tar 2>/dev/null | gzip -dc 2>/dev/null | grep -aq "ZMUX_RUNTIME_LIB_DIR"); then
+            echo "FAIL: runtime self-heal marker ZMUX_RUNTIME_LIB_DIR not in private.tar"; FAIL=1
+          fi
+          EXPECT_SHA=$(git rev-parse HEAD)
+          if ! (unzip -p "$APK" assets/private.tar 2>/dev/null | gzip -dc 2>/dev/null | grep -aq "$EXPECT_SHA"); then
+            echo "FAIL: build marker $EXPECT_SHA not in APK"; FAIL=1
+          fi
+          if [ "$FAIL" != 0 ]; then
+            echo "::error::APK verification failed - stale or unfixed artifact (see log above)."
+            exit 1
+          fi
+          echo "[PASS] APK verified: NEEDED=libtalloc.so, libtalloc.so shipped, runtime marker + build SHA present"
+
       - name: Name and checksum APK
         id: artifact
         run: |
