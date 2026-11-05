package android.widget;
import android.content.Context;
import android.util.AttributeSet;
import android.view.ViewGroup;
public class FrameLayout extends ViewGroup {
 public FrameLayout(Context c){super(c);} public FrameLayout(Context c,AttributeSet a){super(c,a);}
 public static class LayoutParams extends ViewGroup.MarginLayoutParams {
  public LayoutParams(int w,int h){super(w,h);} public LayoutParams(int w,int h,int g){super(w,h); gravity=g;}
  public int gravity;
  public static final int MATCH_PARENT=-1; public static final int WRAP_CONTENT=-2;
 }
}
