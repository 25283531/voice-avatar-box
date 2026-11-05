package android.widget;
import android.content.Context;
import android.util.AttributeSet;
import android.view.View;
public class TextView extends View {
 public TextView(Context c){super(c);} public TextView(Context c,AttributeSet a){super(c,a);}
 public void setText(CharSequence t){} public void setText(int r){}
 public CharSequence getText(){return null;}
 public void setTextSize(float s){} public void setTextSize(int unit,float s){} public void setTextColor(int c){}
 public void setGravity(int g){} public int getGravity(){return 0;}
 public void setTypeface(android.graphics.Typeface t){} public android.graphics.Typeface getTypeface(){return null;}
 public void setIncludeFontPadding(boolean b){} public boolean getIncludeFontPadding(){return true;}
 public void setLineSpacing(float a,float b){} public void setLetterSpacing(float s){}
 public void setMaxLines(int n){} public int getMaxLines(){return 1;} public void setAllCaps(boolean b){}
}
