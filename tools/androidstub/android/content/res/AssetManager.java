package android.content.res;

/**
 * 最小 `AssetManager` stub —— 签名照抄真 Android。
 *
 * 本项目只把 AssetManager 作为**句柄**传给 sherpa-onnx 的 JNI
 * （`newFromAsset(assetManager, config)`），Kotlin 侧不调用它的任何方法。
 * 所以这里方法体返回空即可，但**类名与包名必须一致**：JNI 侧会按
 * `android/content/res/AssetManager` 去查对象类型。
 */
public class AssetManager {
    public java.io.InputStream open(String name) throws java.io.IOException { return null; }
    public String[] list(String path) throws java.io.IOException { return new String[0]; }
}
