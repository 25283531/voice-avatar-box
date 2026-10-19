import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import org.jetbrains.kotlin.gradle.tasks.KotlinCompile

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.elder.companion"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.elder.companion"
        minSdk = 24
        targetSdk = 34
        // ⚠️ 这里默认值为**唯一真源**，workflow_dispatch 的 inputs 只在显式传入时覆盖。
        //   发版时改这里一处即可（yml 不再兜默认值 —— 两份真源会互相覆盖，见 D39）。
        //
        // ⚠️⚠️ 刻意用 `takeIf { it.isNotBlank() }` 而不是直接 `?:`：
        //   `?:` 只在 **null** 时兜底，而 CI 若传 `-PVERSION_NAME=""`
        //   （push 触发时 inputs 为空，很容易顺手传个空串），
        //   gradle 里就**真的存在一个值为空串的属性** ⇒ 兜底失效
        //   ⇒ versionName 变成空串、产物名变成 `elder-companion--110.apk`，
        //   而构建照样绿灯。2026-10-19 真实踩过一次。
        //   空串与 null 同义，是这里必须显式写出来的一句。
        versionCode = (project.findProperty("VERSION_CODE") as String?)
            ?.takeIf { it.isNotBlank() }?.toIntOrNull() ?: 110
        versionName = (project.findProperty("VERSION_NAME") as String?)
            ?.takeIf { it.isNotBlank() } ?: "1.1.0"
    }

    signingConfigs {
        // 正式签名：四个环境变量齐全时才启用，否则回落到 debug 签名（依然可安装）
        create("release") {
            val ksFile = System.getenv("ANDROID_KEYSTORE_FILE")
            if (!ksFile.isNullOrBlank() && file(ksFile).exists()) {
                storeFile = file(ksFile)
                storePassword = System.getenv("ANDROID_STORE_PASSWORD")
                keyAlias = System.getenv("ANDROID_KEY_ALIAS")
                keyPassword = System.getenv("ANDROID_KEY_PASSWORD")
                // 按 magic 判断类型：JKS 是 FE ED FE ED，其余（含 Android 生成的）按 PKCS12
                val head = file(ksFile).readBytes().take(4).toByteArray()
                val jksMagic = byteArrayOf(0xFE.toByte(), 0xED.toByte(), 0xFE.toByte(), 0xED.toByte())
                storeType = if (head.contentEquals(jksMagic)) "jks" else "pkcs12"
            }
        }
    }

    buildTypes {
        getByName("release") {
            isMinifyEnabled = false
            val release = signingConfigs.getByName("release")
            signingConfig = if (release.storeFile != null) release else signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

// Kotlin 默认 jvmTarget 是 1.8，与 Java 17 的 compileOptions 不一致会直接构建失败
tasks.withType<KotlinCompile>().configureEach {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    // WebViewAssetLoader：把 assets 里的 H5 映射成一个虚拟 https 域来加载，
    // 这样页面有正常 origin，localStorage / 跨域请求行为与线上 H5 一致
    implementation("androidx.webkit:webkit:1.8.0")
}

// 供 CI 读取**实际生效**的版本号（走的是与 assembleRelease 完全相同的取值逻辑）。
// 存在的理由：CI 需要给产物起一个带版本号的名字，若 CI 自己再写一份版本号，
// 就会与这里漂移 —— 文件名写着 1.1.0、包里却是 100，安卓侧判为低版本拒装，
// 而构建全程绿灯。**让程序自己报出它的版本，是消除两份真源最省事的办法。**
//
// ⚠️ 刻意**不**用 `com.android.build.gradle.AppExtension` 读回 defaultConfig：
//   该类在新版 AGP 已被移除/私有化，引用它会让整个构建脚本编译失败 ——
//   为了打一行日志而搞挂发版链路，代价比收益大得多。
//   这里改为复算同一套取值逻辑（属性 → 默认值），与上面 defaultConfig 完全一致；
//   真正的"一致性由 CI 断言"靠下面的读回校验：值对不上时 CI 会报错而不是静默发包。
tasks.register("printElderVersion") {
    val vCode = (project.findProperty("VERSION_CODE") as String?)
        ?.takeIf { it.isNotBlank() }?.toIntOrNull() ?: 110
    val vName = (project.findProperty("VERSION_NAME") as String?)
        ?.takeIf { it.isNotBlank() } ?: "1.1.0"
    doLast {
        println("ELDER_VERSION $vName $vCode")
    }
}
