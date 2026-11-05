package android.view;
public abstract class Choreographer {
 public static Choreographer getInstance(){return null;}
 public abstract void postFrameCallback(FrameCallback cb); public abstract void removeFrameCallback(FrameCallback cb);
 public interface FrameCallback { void doFrame(long frameTimeNanos); }
}
