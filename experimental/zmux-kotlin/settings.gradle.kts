pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // Termux terminal-view / terminal-emulator artifacts are published here.
        maven("https://jitpack.io")
    }
}

rootProject.name = "ZmuxKotlin"
include(":app")
