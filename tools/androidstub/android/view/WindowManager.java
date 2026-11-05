package android.view;
public interface WindowManager {
 class LayoutParams { public static final int FLAG_FULLSCREEN=0x400; public static final int FLAG_KEEP_SCREEN_ON=0x80;
  public LayoutParams(){} public int flags; public float screenBrightness; }
}
