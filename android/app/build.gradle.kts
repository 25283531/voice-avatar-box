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
        versionCode = (project.findProperty("VERSION_CODE") as String?)?.toIntOrNull() ?: 100
        versionName = (project.findProperty("VERSION_NAME") as String?) ?: "1.0.0"
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
