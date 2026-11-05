package android.app;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
public class Activity extends Context {
 public void onCreate(Bundle b){} public void onDestroy(){}
 public void setContentView(View v){} public void setContentView(int r){}
 public View findViewById(int id){return null;}
 public Window getWindow(){return null;}
 public void runOnUiThread(Runnable r){r.run();}
 public void startActivity(Intent i){}
 public boolean dispatchKeyEvent(KeyEvent e){return false;}
 public void onRequestPermissionsResult(int c,String[] p,int[] g){}
 public boolean onKeyLongPress(int k,KeyEvent e){return false;}
 public void onWindowFocusChanged(boolean b){}
 public boolean isFinishing(){return false;}
 public Object getSystemService(String name){return null;}
 public android.content.pm.PackageManager getPackageManager(){return null;}
 public void registerReceiver(android.content.BroadcastReceiver r,android.content.IntentFilter f){}
 public void unregisterReceiver(android.content.BroadcastReceiver r){}
 public int checkSelfPermission(String p){return 0;}
 public void requestPermissions(String[] p,int c){}
 public void setResult(int c,Intent i){}
}
