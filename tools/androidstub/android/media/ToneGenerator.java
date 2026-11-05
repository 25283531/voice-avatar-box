package android.media;

/**
 * 最小 `ToneGenerator` stub —— 签名照抄真 Android。
 *
 * ⚠️ 下面的常量值**参与了真机行为**（传给 `startTone`），所以不能随便填。
 *    这里照抄 AOSP `ToneGenerator.java` 的公开取值；若哪天要新增，
 *    必须回 AOSP 核对，别按"看起来顺"的数字写。
 */
public class ToneGenerator {

    public static final int TONE_CDMA_ALERT_CALL_GUARD = 16;
    public static final int TONE_PROP_BEEP = 24;
    public static final int TONE_PROP_ACK = 25;
    public static final int TONE_PROP_NACK = 26;
    public static final int TONE_PROP_PROMPT = 27;
    public static final int TONE_PROP_BEEP2 = 28;
    public static final int TONE_SUP_ERROR = 55;

    public ToneGenerator(int streamType, int volume) { }

    public boolean startTone(int toneType) { return false; }

    public boolean startTone(int toneType, int durationMs) { return false; }

    public void stopTone() { }

    public void release() { }
}
