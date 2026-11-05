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
}
