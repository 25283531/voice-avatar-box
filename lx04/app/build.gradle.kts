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

    /**
     * 发布密钥：有 `-PLX04_STORE_FILE=...` 就用它，没有就退回 debug 密钥。
     *
     * ⚠️ **设备端 APK 必须签名**，否则 `adb install` 直接
     * `INSTALL_PARSE_FAILED_NO_CERTIFICATES`（不是警告，是装不上）。
     * 默认 `assembleRelease` 出的是 `app-release-unsigned.apk`，
     * 这个文件在 CI 里能构建成功、却装不进音箱 —— 属于"绿灯但没用"的产物。
     *
     * 密钥不进仓库：CI 从 `LX04_KEYSTORE_BASE64` 解出来放临时目录。
     * 没有密钥时退回 debug 签名：APK 能装、能验证功能，代价是换一台构建机
     * 签名就变，升级前得先卸载。bring-up 阶段够用。
     */
    val deviceStoreFile = (project.findProperty("LX04_STORE_FILE") as String?)
        ?.takeIf { it.isNotBlank() }

    /**
     * 仓库内固定构建密钥 —— 没有外部密钥时用它兜底。
     *
     * ## 为什么必须有（不是"省事"）
     *
     * 不指定 keystore 时 AGP 会用 `~/.android/debug.keystore`；而 CI 每次都是
     * 干净环境，那个文件是**重新生成**的 ⇒ 每次出包的签名都不同 ⇒
     * `adb install -r` 报 `INSTALL_FAILED_UPDATE_INCOMPATIBLE` ⇒ 只能卸载重装
     * ⇒ `/data` 被清空。
     *
     * 而设备端所有有价值的状态（提醒表、token、时钟偏移）都在 `/data`。
     * 签名不稳 ⇒ 每次装机都从零开始 ⇒ 「持久化」在验收阶段等于不存在。
     * 另外 Android 8.0+ 的 `ANDROID_ID` 是按签名派生的，签名一变它就变，
     * 绑定关系会漂成孤儿设备（虽然有 MAC 兜底，但落到 ANDROID_ID 时就废了）。
     *
     * 这个密钥不是秘密、不承担身份作用，密码就写在这里（见 .gitignore 末尾说明）。
     * 生产环境仍走 `LX04_KEYSTORE_BASE64` secret，优先级更高。
     */
    val bundledKeystore = rootProject.file("keystore/lx04-build.keystore")
    val useBundled = deviceStoreFile == null && bundledKeystore.exists()

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

    signingConfigs {
        // 只在真的给了密钥文件时才创建这个配置。
        // 无条件 create 的话，AGP 会因为 storeFile 为空报
        // "SigningConfig is missing required property storeFile"。
        if (deviceStoreFile != null) {
            create("device") {
                storeFile = file(deviceStoreFile)
                storePassword = project.findProperty("LX04_STORE_PASSWORD") as String?
                keyAlias = (project.findProperty("LX04_KEY_ALIAS") as String?) ?: "lx04"
                keyPassword = project.findProperty("LX04_KEY_PASSWORD") as String?
            }
        }
        // 仓库内固定密钥（见上面 bundledKeystore 的说明）
        if (useBundled) {
            create("bundled") {
                storeFile = bundledKeystore
                storePassword = "lx04build"
                keyAlias = "lx04"
                keyPassword = "lx04build"
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            // 见上：宁可 debug 签名，也不能出未签名的包（装不上）。
            // 顺序：外部密钥 > 仓库内固定密钥 > AGP 的随机 debug 签名。
            signingConfig = when {
                deviceStoreFile != null -> signingConfigs.getByName("device")
                useBundled -> signingConfigs.getByName("bundled")
                else -> signingConfigs.getByName("debug")
            }
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

    testOptions {
        unitTests {
            /**
             * 让 JVM 单测能直接调 `android.util.Log`。
             *
             * 默认行为是抛 "Method ... not mocked" —— 于是 [OpusCodec]
             * 这种用了 Log 的类**根本没法在单测里跑**，只能靠
             * "把 Log 抽出去"来绕，而那会为了测试而改生产代码的结构。
             *
             * `isReturnDefaultValues = true` 让 android.* 的方法返回默认值
             * （Log.i 返回 0、不抛异常），于是单测可以直接测 [OpusCodec]：
             * 并发编码、失败自愈、计数 —— 全都是这次实机故障的关键路径。
             */
            isReturnDefaultValues = true
        }
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
