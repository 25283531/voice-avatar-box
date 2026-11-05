package android.os;

/**
 * 最小 `Handler` stub —— 签名照抄真 Android。
 *
 * ⚠️ `post` / `postDelayed` 返回 **boolean**、`removeCallbacks` 返回 **void**。
 *    写错返回类型在这里不会报错（Kotlin 调用的返回值可以丢弃），
 *    但真 SDK 上会编译失败 —— 属于"stub 比真 SDK 宽松"那类假绿。
 */
public class Handler {

    public Handler() { }

    public Handler(Looper looper) { }

    public Handler(boolean async) { }

    public final boolean post(Runnable r) { return false; }

    public final boolean postDelayed(Runnable r, long delayMillis) { return false; }

    public final void removeCallbacks(Runnable r) { }

    public final void removeCallbacksAndMessages(Object token) { }

    public final Looper getLooper() { return null; }
}
