package android.view;
import android.content.Context;
import android.util.AttributeSet;
public class View {
 public static final int VISIBLE=0; public static final int INVISIBLE=4; public static final int GONE=8;
 public static final int SYSTEM_UI_FLAG_FULLSCREEN=0x4; public static final int SYSTEM_UI_FLAG_HIDE_NAVIGATION=0x2;
 public static final int SYSTEM_UI_FLAG_IMMERSIVE_STICKY=0x1000; public static final int SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN=0x400;
 public static final int SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION=0x200; public static final int SYSTEM_UI_FLAG_LAYOUT_STABLE=0x100;
 public void setSystemUiVisibility(int v){} public int getSystemUiVisibility(){return 0;}
 public View(Context c){} public View(Context c,AttributeSet a){} public View(Context c,AttributeSet a,int d){}
 public void setVisibility(int v){} public int getVisibility(){return VISIBLE;}
 public void setBackgroundColor(int c){} public void invalidate(){}
 public void setPadding(int a,int b,int c,int d){}
 public int getWidth(){return 0;} public int getHeight(){return 0;}
 public void post(Runnable r){}
 public boolean performClick(){return false;}
 public void setLayoutParams(ViewGroup.LayoutParams p){}
 public ViewGroup.LayoutParams getLayoutParams(){return null;}
 public android.content.res.Resources getResources(){return null;}
 public void setTranslationX(float v){} public void setTranslationY(float v){}
 public void setAlpha(float v){} public void setRotation(float v){} public void setScaleX(float v){} public void setScaleY(float v){}
 public void setContentDescription(CharSequence c){} public void setOnClickListener(OnClickListener l){}
 public float getTranslationX(){return 0f;} public float getAlpha(){return 1f;}
 public void setImportantForAccessibility(int v){} public void setFocusableInTouchMode(boolean b){}
 public void setContentDescription(int r){}
 protected void onDraw(android.graphics.Canvas c){}
 protected void onVisibilityChanged(android.view.View changed,int visibility){}
 protected void onVisibilityChanged(android.view.View changed,int visibility,int oldVisibility){}
 public void postInvalidateOnAnimation(){} public void postInvalidate(){} public void invalidate()
 public void setWillNotDraw(boolean b){}
 protected void onAttachedToWindow(){} protected void onDetachedFromWindow(){}
 public interface OnClickListener { void onClick(View v); }
}
