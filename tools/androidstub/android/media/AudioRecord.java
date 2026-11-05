package android.media;
public class AudioRecord {
 public static final int STATE_INITIALIZED=1; public static final int STATE_UNINITIALIZED=0;
 public static final int RECORDSTATE_RECORDING=3; public static final int RECORDSTATE_STOPPED=1;
 public static final int ERROR_INVALID_OPERATION=-3; public static final int ERROR_BAD_VALUE=-2; public static final int ERROR_DEAD_OBJECT=-6;
 public AudioRecord(int src,int rate,int ch,int fmt,int buf){}
 public int getState(){return STATE_INITIALIZED;} public int getRecordingState(){return RECORDSTATE_RECORDING;}
 public int getAudioSessionId(){return 0;}
 /** 自创建以来读取的帧数（真 API 23+）。 */
 public int getReadCount(){return 0;}
 public void startRecording(){} public void stop(){} public void release(){}
 public int read(short[] b,int o,int l){return 0;} public int read(byte[] b,int o,int l){return 0;}
 public static int getMinBufferSize(int r,int c,int f){return 4096;}
}
