package android.widget;
import android.content.Context;
import android.util.AttributeSet;
import android.view.ViewGroup;
public class LinearLayout extends ViewGroup {
 public LinearLayout(Context c){super(c);} public LinearLayout(Context c,AttributeSet a){super(c,a);}
 public static final int VERTICAL=1; public static final int HORIZONTAL=0;
 public static final int MATCH_PARENT=-1; public static final int WRAP_CONTENT=-2;
 public void setOrientation(int o){} public int getOrientation(){return VERTICAL;}
 public void setGravity(int g){} public int getGravity(){return 0;}
 public static class LayoutParams extends ViewGroup.LayoutParams {
  public LayoutParams(int w,int h){super(w,h);}
  public LayoutParams(int w,int h,float weight){super(w,h);}
  public float weight;
 }
}
