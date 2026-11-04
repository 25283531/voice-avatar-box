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
        // ⚠️ 这里必须是**字符串**，不能写成 JvmTarget.JVM_1_8。
        //   Kotlin 1.9.x 的 `kotlinOptions.jvmTarget` 是 String 类型；
        //   JvmTarget 枚举住在 org.jetbrains.kotlin.gradle.dsl 下（不是 tasks），
        //   而且只在 `kotlin { compilerOptions { ... } }` 新 DSL 里才吃枚举。
        //   之前误写 `org.jetbrains.kotlin.gradle.tasks.JvmTarget` →
        //   `Unresolved reference: JvmTarget`，构建在第 1 行就挂（run 41842550769）。
        //   且 jvmTarget 必须与上面 compileOptions 的 1.8 保持一致，否则 AGP 直接报错。
        jvmTarget = "1.8"
    }

    packaging {
        resources {
            excludes += setOf("META-INF/*.kotlin_module", "META-INF/DEPENDENCIES")
        }
    }

    lint {
        /**
         * 关掉 `ExpiredTargetSdkVersion`。
         *
         * 这条检查的内容是「Google Play 要求 targetSdk ≥ 33」—— 一条**上架策略**，
         * 与代码质量无关。而这个 App 根本不上架：它是刷进 LX04 的系统级设备端，
         * targetSdk 必须贴着设备的 Android 8.1（API 27）走，理由见文件头。
         *
         * 不关的后果：`assembleRelease` 会跑 `lintVitalRelease`，
         * 它把这条当**致命错误**直接中断打包（CI run 41847758530）。
         */
        disable += "ExpiredTargetSdkVersion"

        // 其余致命问题仍然要拦：宁可打包失败，也不要出一个装上去就崩的 APK。
        abortOnError = true
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
