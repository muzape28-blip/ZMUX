pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
        maven("https://chaquo.com/maven")
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // Termux terminal-view / terminal-emulator artifacts are published here.
        maven("https://jitpack.io")
        maven("https://chaquo.com/maven")
    }
}

rootProject.name = "ZmuxKotlin"
include(":app")
