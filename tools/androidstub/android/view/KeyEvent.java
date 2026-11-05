package android.view;
public class KeyEvent {
 public static final int ACTION_DOWN=0; public static final int ACTION_UP=1;
 public static final int KEYCODE_F1=131; public static final int KEYCODE_VOLUME_UP=24; public static final int KEYCODE_VOLUME_DOWN=25;
 public static final int KEYCODE_POWER=26; public static final int KEYCODE_DPAD_UP=19; public static final int KEYCODE_DPAD_DOWN=20;
 public static final int KEYCODE_DPAD_LEFT=21; public static final int KEYCODE_DPAD_RIGHT=22; public static final int KEYCODE_ENTER=66;
 public static final int KEYCODE_BACK=4; public static final int KEYCODE_HOME=3;
 public int getAction(){return 0;} public int getKeyCode(){return 0;} public int getScanCode(){return 0;}
 public int getDeviceId(){return 0;} public int getRepeatCount(){return 0;} public long getEventTime(){return 0;}
 public boolean isLongPress(){return false;}
 public static final int KEYCODE_UNKNOWN=0;
}
