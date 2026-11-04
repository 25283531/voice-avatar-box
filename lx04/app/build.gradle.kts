import org.jetbrains.kotlin.gradle.tasks.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

/**
 * 小爱触屏音箱 LX04 —— 设备端原生客户端。
 *
 * 与仓库里的 `android/`（家属端 WebView 壳）是**两个独立应用**：
 *   · `android/`  = 家属用的手机 App，H5 + WebView
 *   · `lx04/`     = 装在音箱上的设备端，纯原生（要碰 AudioRecord / 按键 / 自绘色带）
 *
 * ⚠️ 架构与 ABI 的两个硬约束（详见 docs/lx04/LX04移植方案.md）：
 *
 * 1. **armeabi-v7a，不能是 arm64**。
 *    LX04 国行系统是 32 位用户空间（Android 8.1 / SDK 27 / armeabi-v7a）。
 *    带 arm64 的 .so 在这个系统上加载会直接 crash。
 *    装 arm64 库进 APK 也会让安装包在 32 位系统上装不上。
 *
 * 2. **minSdk 27 / targetSdk 27**，刻意不追新。
 *    · 27 = LX04 原厂 Android 8.1，正好对齐，不用做任何版本兼容分支
 *    · **不要升到 28+**：Android 9 引入后台麦克风限制，
 *      Android 10 引入 FOREGROUND_SERVICE_MICROPHONE。
 *      我们要的是「常驻录音做唤醒」，8.1 反而是最省事的一代。
 *    · compileSdk 可以更高（构建工具链需要），但 targetSdk 决定运行时行为。
 */
android {
    namespace = "com.elder.lx04"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.elder.lx04"
        minSdk = 27
        targetSdk = 27
        versionCode = (project.findProperty("VERSION_CODE") as String?)
            ?.takeIf { it.isNotBlank() }?.toIntOrNull() ?: 1
        versionName = (project.findProperty("VERSION_NAME") as String?)
            ?.takeIf { it.isNotBlank() } ?: "0.1.0"

        // ⚠️ 见上：只保留 32 位 ARM。混进 arm64/x86 会在 LX04 上装不上或加载失败。
        ndk {
            abiFilters += listOf("armeabi-v7a")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
        debug {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_1_8
        targetCompatibility = JavaVersion.VERSION_1_8
    }

    kotlinOptions {
        jvmTarget = JvmTarget.JVM_1_8
    }

    packaging {
        resources {
            excludes += setOf("META-INF/*.kotlin_module", "META-INF/DEPENDENCIES")
        }
    }

    // Opus 的 .so 不做 strip：JNI 符号解析依赖它（strip 后常见 UnsatisfiedLinkError）
    packagingOptions {
        jniLibs {
            useLegacyPackaging = true
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    testImplementation("junit:junit:4.13.2")
}
