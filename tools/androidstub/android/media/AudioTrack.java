package android.media;
public class AudioTrack {
 public static final int MODE_STREAM=1; public static final int STATE_INITIALIZED=1; public static final int STATE_UNINITIALIZED=0;
 public static final int PLAYSTATE_PLAYING=3; public static final int PLAYSTATE_STOPPED=1;
 public AudioTrack(int stream,int rate,int ch,int fmt,int buf,int mode){}
 public int getState(){return STATE_INITIALIZED;} public int getPlayState(){return PLAYSTATE_PLAYING;}
 public void play(){} public void pause(){} public void stop(){} public void flush(){} public void release(){}
 public int write(short[] b,int o,int l){return l;} public int write(byte[] b,int o,int l){return l;}
 public static int getMinBufferSize(int r,int c,int f){return 4096;}
}
