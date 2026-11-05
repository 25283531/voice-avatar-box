package android.media;

/**
 * 最小 `AudioManager` stub —— 签名与常量**照抄真 Android**。
 *
 * ⚠️ 只放本项目真的用到的成员。加之前先确认真 SDK 里有它、
 *    且签名（含返回类型、是否 static）一致 —— stub 比真 SDK 宽松
 *    会让本地编译全绿、CI 用真 SDK 编译直接红。这个项目已踩过两次。
 */
public class AudioManager {

    /** 真值照抄真 Android（STREAM_MUSIC = 3）。 */
    public static final int STREAM_VOICE_CALL = 0;
    public static final int STREAM_SYSTEM = 1;
    public static final int STREAM_RING = 2;
    public static final int STREAM_MUSIC = 3;
    public static final int STREAM_ALARM = 4;
    public static final int STREAM_NOTIFICATION = 5;

    /** `setStreamVolume` 的 flags 位（真值照抄）。 */
    public static final int FLAG_SHOW_UI = 1;
    public static final int FLAG_ALLOW_RINGER_MODES = 2;
    public static final int FLAG_PLAY_SOUND = 4;
    public static final int FLAG_REMOVE_SOUND_AND_VIBRATE = 8;
    public static final int FLAG_VIBRATE = 16;

    public int getStreamMaxVolume(int streamType) { return 0; }

    public int getStreamVolume(int streamType) { return 0; }

    public void setStreamVolume(int streamType, int index, int flags) { }
}
