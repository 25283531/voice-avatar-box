package android.content;
public class Intent {
 public Intent(){} public Intent(String a){} public Intent(String a,Uri b){}
 public Intent(Context c,Class<?> cls){}
 public static final String ACTION_BOOT_COMPLETED="android.intent.action.BOOT_COMPLETED";
 public static final String ACTION_MAIN="android.intent.action.MAIN";
 public static final String ACTION_LOCKED_BOOT_COMPLETED="android.intent.action.LOCKED_BOOT_COMPLETED";
 public static final String ACTION_QUICKBOOT_POWERON="android.intent.action.QUICKBOOT_POWERON";
 public static final String ACTION_BATTERY_CHANGED="android.intent.action.BATTERY_CHANGED";
 public static final String ACTION_POWER_CONNECTED="android.intent.action.ACTION_POWER_CONNECTED";
 public Intent setAction(String a){return this;}
 public Intent putExtra(String k,String v){return this;}
 public Intent putExtra(String k,boolean v){return this;}
 public Intent addFlags(int f){return this;}
 public static final int FLAG_ACTIVITY_NEW_TASK=0x10000000; public static final int FLAG_ACTIVITY_SINGLE_TOP=0x20000000;
 public static final int FLAG_ACTIVITY_REORDER_TO_FRONT=0x20000; public static final int FLAG_ACTIVITY_CLEAR_TOP=0x04000000;
 public static final int FLAG_ACTIVITY_CLEAR_TASK=0x00008000; public static final int FLAG_ACTIVITY_NO_HISTORY=0x40000000;
 public Intent setClassName(String p,String c){return this;}
 public Intent setComponent(android.content.ComponentName c){return this;}
 public String getStringExtra(String k){return null;}
 public int getIntExtra(String k,int d){return d;}
 public boolean getBooleanExtra(String k,boolean d){return d;}
 public long getLongExtra(String k,long d){return d;}
 public java.util.Set<String> getExtras(){return null;}
 public String getAction(){return null;}
}
