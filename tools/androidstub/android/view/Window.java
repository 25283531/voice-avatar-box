package android.view;
public abstract class Window { public WindowManager.LayoutParams getAttributes(){return new WindowManager.LayoutParams();} public void setAttributes(WindowManager.LayoutParams p){} public void addFlags(int f){} public void setDecorFitsSystemWindows(boolean b){}
 public View getDecorView(){return null;} public void setStatusBarColor(int c){} public void setNavigationBarColor(int c){} }
