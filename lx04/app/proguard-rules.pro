# LX04 设备端客户端的混淆规则。
#
# 当前 release 构建 isMinifyEnabled = false（见 app/build.gradle.kts），
# 所以这个文件暂时不生效 —— 但 AGP 会引用它，缺文件会让构建配置阶段报错，
# 因此必须存在。
#
# 未来真要开混淆时，下面这几条是必须保留的：
#
#   · JNI 方法：Opus 编解码走 native，方法名被混淆后 native 侧找不到符号
#     → java.lang.UnsatisfiedLinkError
#   · 反射/序列化用到的协议类：字段名参与 JSON key
#   · Kotlin 协程的 Continuation 相关合成类

# --- JNI（Opus 编解码） ---
-keepclasseswithmembernames class * {
    native <methods>;
}

# --- 协议数据类（字段名即 JSON key，混淆后会和服务端对不上） ---
-keep class com.elder.lx04.voice.** { *; }

# --- Kotlin 元数据（协程、反射需要） ---
-keepattributes *Annotation*, InnerClasses, Signature, Exceptions
-dontwarn kotlinx.coroutines.**
