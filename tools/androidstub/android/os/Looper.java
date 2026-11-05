package android.os;

/**
 * 最小 `Looper` stub —— 签名照抄真 Android。
 *
 * 只放本项目用到的成员（`getMainLooper()`）。所有方法返回 null 即可：
 * stub 只参与**编译**，不参与运行。
 */
public class Looper {

    public static Looper getMainLooper() { return null; }

    public static Looper myLooper() { return null; }

    public void quit() { }

    public void quitSafely() { }

    public Thread getThread() { return null; }
}
