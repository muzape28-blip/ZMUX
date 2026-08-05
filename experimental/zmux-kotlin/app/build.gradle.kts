plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.zmux.terminal"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.zmux.terminal"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0-poc"

        ndk {
            // Target: ARMv7 / Android Go class devices (Infinix Smart 9 HD) + modern arm64.
            abiFilters += listOf("armeabi-v7a", "arm64-v8a")
        }
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
            applicationIdSuffix = ".debug"
        }
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = false
    }

    packaging {
        // PRoot is an executable ELF which Android must materialize in
        // ApplicationInfo.nativeLibraryDir. Compressed-in-APK native libs are
        // loadable but not reliably exec-able on modern Android.
        jniLibs {
            useLegacyPackaging = true
        }
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }

    sourceSets {
        getByName("main") {
            // Explicit rather than relying on AGP's default: the generated
            // PRoot artifacts must be picked up before packageDebug runs.
            jniLibs.srcDir("src/main/jniLibs")
        }
    }
}

val prootAbis = listOf("armeabi-v7a", "arm64-v8a")
val prootLibraries = listOf("libproot.so", "libproot-loader.so", "libtalloc.so")
val prootJniDir = layout.projectDirectory.dir("src/main/jniLibs")

fun requireProotArtifacts() {
    val missing = prootAbis.flatMap { abi ->
        prootLibraries.mapNotNull { library ->
            val artifact = prootJniDir.file("$abi/$library").asFile
            if (artifact.isFile && artifact.length() > 0) null else artifact.path
        }
    }
    check(missing.isEmpty()) {
        "PRoot native artifacts are required but missing: ${missing.joinToString()}. " +
            "Install Android NDK 29 or set ANDROID_NDK_HOME; do not ship a shell-only APK."
    }
}

tasks.register<Exec>("buildProot") {
    val ndkHome = System.getenv("ANDROID_NDK_LATEST_HOME") ?: System.getenv("ANDROID_NDK_HOME")
    if (ndkHome.isNullOrBlank()) {
        throw GradleException(
            "Android NDK is required to build ZMUX PRoot. Set ANDROID_NDK_LATEST_HOME or " +
                "ANDROID_NDK_HOME; producing an APK without libproot.so is forbidden."
        )
    }
    println("Building PRoot using NDK at: $ndkHome")
    commandLine(
        "python3", "../scripts/build_proot_android.py", "--ndk", ndkHome,
        "--out", "src/main/jniLibs", "--abis", "armeabi-v7a,arm64-v8a",
    )
    doLast { requireProotArtifacts() }
}

tasks.register("verifyProotJni") {
    dependsOn("buildProot")
    doLast { requireProotArtifacts() }
}

tasks.named("preBuild") {
    dependsOn("verifyProotJni")
}

// Assert the actual *APK* contents as well as the build directory contents.
// This catches source-set/packaging regressions before a phone sees the APK.
tasks.register("verifyDebugProotPackage") {
    dependsOn("packageDebug")
    doLast {
        val apk = layout.buildDirectory.file("outputs/apk/debug/app-debug.apk").get().asFile
        check(apk.isFile) { "debug APK was not produced: $apk" }
        java.util.zip.ZipFile(apk).use { zip ->
            val missing = prootAbis.flatMap { abi ->
                prootLibraries.mapNotNull { library ->
                    val entry = "lib/$abi/$library"
                    if (zip.getEntry(entry) == null) entry else null
                }
            }
            check(missing.isEmpty()) {
                "APK is missing mandatory PRoot libraries: ${missing.joinToString()}"
            }
        }
    }
}

tasks.named("assembleDebug") {
    finalizedBy("verifyDebugProotPackage")
}

chaquopy {
    defaultConfig {
        version = "3.11"
        // Android's Python runtime doesn't reliably expose the device CA store.
        // linuxenv downloads only through a certificate-verifying context backed
        // by this bundled CA set; never fall back to unverified TLS.
        pip {
            install("certifi==2025.8.3")
        }
        // CI runners may only provide a newer host Python. Disabling build-time
        // bytecode keeps the app on the supported Python 3.11 runtime (including
        // armeabi-v7a) instead of failing on a host-Python minor mismatch.
        // Chaquopy compiles these sources safely on the device at first use.
        pyc {
            src = false
            pip = false
            stdlib = false
        }
    }
}

dependencies {
    // --- Termux terminal stack (Apache-2.0 libraries only, NOT the GPLv3 app) ---
    //
    // Coordinates per termux-app's "Termux Libraries" wiki page:
    //   * JitPack groupId is `com.termux.termux-app`
    //   * `com.termux` also resolves (termux.com carries the JitPack DNS TXT record)
    //   * terminal-emulator is a transitive dep of terminal-view, but we pin it
    //     explicitly so a version skew fails at resolution instead of at runtime.
    // Versions >= 0.116 are published; 0.118.0 is the last stable tag.
    implementation("com.termux.termux-app:terminal-view:0.118.0")
    implementation("com.termux.termux-app:terminal-emulator:0.118.0")

    // --- Transport to the existing Python ZMUX backend ---
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // --- AndroidX (kept minimal for Android Go footprint) ---
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
