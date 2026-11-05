package android.provider;
public final class Settings {
 public static final class Global { public static int getInt(android.content.Context c,String k,int d){return d;} public static boolean putInt(android.content.Context c,String k,int v){return true;} }
 public static final class Secure { public static final String ANDROID_ID="android_id"; public static String getString(android.content.ContentResolver r,String k){return null;} }
 public static final class System { public static int getInt(android.content.Context c,String k,int d){return d;} }
}
