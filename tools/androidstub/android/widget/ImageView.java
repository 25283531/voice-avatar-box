package android.widget;
import android.content.Context;
import android.util.AttributeSet;
import android.view.View;
public class ImageView extends View {
 public ImageView(Context c){super(c);} public ImageView(Context c,AttributeSet a){super(c,a);}
 public void setImageResource(int r){}
 public void setScaleType(ScaleType t){} public ScaleType getScaleType(){return ScaleType.FIT_CENTER;}
 public static class ScaleType { public static final ScaleType FIT_CENTER=new ScaleType(); public static final ScaleType FIT_XY=new ScaleType(); public static final ScaleType CENTER_CROP=new ScaleType(); }
}
