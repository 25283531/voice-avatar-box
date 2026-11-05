package android.content;

/**
 * 最小 `SharedPreferences` stub —— 签名**照抄真 Android**。
 *
 * ⚠️ 加方法前必须确认真 SDK 有这个方法、且签名一致（返回类型也算签名）。
 *    stub 比真 SDK 宽松 ⇒ 本地编译全绿、CI 用真 SDK 编译直接红，
 *    这个项目已经踩过两次（`AudioRecord.getReadCount`、`VOICE_RECOGNITION=7`）。
 */
public interface SharedPreferences {

    String getString(String key, String defValue);

    long getLong(String key, long defValue);

    int getInt(String key, int defValue);

    boolean getBoolean(String key, boolean defValue);

    boolean contains(String key);

    Editor edit();

    interface Editor {
        Editor putString(String key, String value);

        Editor putLong(String key, long value);

        Editor putInt(String key, int value);

        Editor putBoolean(String key, boolean value);

        Editor remove(String key);

        Editor clear();

        /** 同步落盘。 */
        boolean commit();

        /** 异步落盘。 */
        void apply();
    }
}
