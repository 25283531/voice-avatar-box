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
 public static final int KEYCODE_APP_SWITCH=187;
 public static final int KEYCODE_F=144;
 public static final int KEYCODE_TAB=61;
 public static final int KEYCODE_SPACE=62;
 public static final int KEYCODE_DEL=67;
 public static final int KEYCODE_SEARCH=84;
 public static final int KEYCODE_MENU=82;
 public static final int KEYCODE_NOTIFICATION=83;
 public static final int KEYCODE_MEDIA_PLAY_PAUSE=85;
 public static final int KEYCODE_CAMERA=27;
 public static final int KEYCODE_CALL=5;
 public static final int KEYCODE_ENDCALL=6;
 public static final int KEYCODE_SETTINGS=176;
 public static final int KEYCODE_APP_SWITCH_2=187;
}
