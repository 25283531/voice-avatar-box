package android.media;

/**
 * Android SDK 的最小 stub。
 *
 * ⚠️ 常量值必须**与真 Android 一致**（AOSP media/java/android/media/MediaRecorder.java）：
 *
 *     DEFAULT             = 0
 *     MIC                 = 1
 *     VOICE_UPLINK        = 2
 *     VOICE_DOWNLINK      = 3
 *     VOICE_CALL          = 4
 *     CAMCORDER           = 5
 *     VOICE_RECOGNITION   = 6      ← 这里曾经写成 7，是**假绿隐患**：
 *                                     本地编译/单测用 7、真机用 6，
 *                                     两边跑的其实是不同的音频源。
 *     VOICE_COMMUNICATION = 7
 *     REMOTE_SUBMIX       = 8
 *     UNPROCESSED         = 9
 *
 * 依据：developer.android.com/reference/android/media/MediaRecorder.AudioSource
 */
public class MediaRecorder {
    public static final class AudioSource {
        public static final int DEFAULT = 0;
        public static final int MIC = 1;
        public static final int VOICE_UPLINK = 2;
        public static final int VOICE_DOWNLINK = 3;
        public static final int VOICE_CALL = 4;
        public static final int CAMCORDER = 5;
        public static final int VOICE_RECOGNITION = 6;
        public static final int VOICE_COMMUNICATION = 7;
        public static final int REMOTE_SUBMIX = 8;
        public static final int UNPROCESSED = 9;
    }
}
