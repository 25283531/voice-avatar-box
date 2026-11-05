package android.view;
import android.content.Context;
public class ViewGroup extends View {
 public ViewGroup(Context c){super(c);}
 public void addView(View v){} public void addView(View v,int p){}
 public void addView(View v,LayoutParams p){} public void removeView(View v){}
 public static class LayoutParams { public LayoutParams(int w,int h){} public int width; public int height; public int gravity;
  public void setMargins(int l,int t,int r,int b){} public int getTopMargin(){return 0;} public int getBottomMargin(){return 0;} public int getLeftMargin(){return 0;} public int getRightMargin(){return 0;} }
 public static class MarginLayoutParams extends LayoutParams {
  public MarginLayoutParams(int w,int h){super(w,h);}
  public void setMargins(int l,int t,int r,int b){}
  public int getTopMargin(){return 0;} public void setTopMargin(int v){}
  public int getBottomMargin(){return 0;} public void setBottomMargin(int v){}
  public int getLeftMargin(){return 0;} public void setLeftMargin(int v){}
  public int getRightMargin(){return 0;} public void setRightMargin(int v){}
 }
}
