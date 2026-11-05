package android.content;
import android.content.pm.PackageManager;
public abstract class Context {
 public static final String CONNECTIVITY_SERVICE="connectivity";
 public static final String WIFI_SERVICE="wifi";
 public abstract Object getSystemService(String name);
 public abstract PackageManager getPackageManager();
 public abstract void registerReceiver(BroadcastReceiver r, IntentFilter f);
 public abstract void unregisterReceiver(BroadcastReceiver r);
 public abstract void startActivity(Intent i);
 public abstract int checkSelfPermission(String p);
 public abstract void requestPermissions(String[] p,int code);
 public android.content.res.Resources getResources(){return null;}
 public android.content.ContentResolver getContentResolver(){return null;}
 public Context getApplicationContext(){return this;}
 public String getPackageName(){return "";}
 public String getString(int r){return "";}
 public android.content.Intent createIntent(String a,Class<?> c){return null;}
 /** 私有文件目录 —— 签名照抄真 Android（返回 File）。 */
 public java.io.File getFilesDir(){return null;}
 /** 外部私有文件目录 —— 签名照抄真 Android。 */
 public java.io.File getExternalFilesDir(String type){return null;}
 /**
  * 偏好设置 —— 签名照抄真 Android（返回 SharedPreferences）。
  *
  * 这里给**默认实现**而不是 abstract：真 Android 里它是 abstract，但 stub 只要
  * 签名一致就能让调用方编译通过；写成 abstract 会逼所有子类（Activity/Service…）
  * 都补一遍空实现，纯属自找维护成本。
  */
 public SharedPreferences getSharedPreferences(String name, int mode){return null;}
 /** 模式常量，真值照抄真 Android。 */
 public static final int MODE_PRIVATE = 0;
 public static final int MODE_WORLD_READABLE = 1;
 public static final int MODE_WORLD_WRITEABLE = 2;
 public static final int MODE_MULTI_PROCESS = 4;
}
