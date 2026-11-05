package android.media;
public class AudioRecord {
 public static final int STATE_INITIALIZED=1; public static final int STATE_UNINITIALIZED=0;
 public static final int RECORDSTATE_RECORDING=3; public static final int RECORDSTATE_STOPPED=1;
 public static final int ERROR_INVALID_OPERATION=-3; public static final int ERROR_BAD_VALUE=-2; public static final int ERROR_DEAD_OBJECT=-6;
 public AudioRecord(int src,int rate,int ch,int fmt,int buf){}
 public int getState(){return STATE_INITIALIZED;} public int getRecordingState(){return RECORDSTATE_RECORDING;}
 public int getAudioSessionId(){return 0;}
 // ⚠️ 这里**故意没有** getReadCount()：真 Android 的 AudioRecord 并没有这个方法
 //    （getReadCount 是 AudioTrack 的）。2026-11-05 本仓踩过：stub 里凭空加了
 //    getReadCount，本地 kotlinc 全绿，CI 的 gradle 用真 SDK 编译直接报
 //    "Unresolved reference: readCount"。
 //    ⇒ 往 stub 里加 API 前，必须先确认真 SDK 里真的有。
 public void startRecording(){} public void stop(){} public void release(){}
 public int read(short[] b,int o,int l){return 0;} public int read(byte[] b,int o,int l){return 0;}
 public static int getMinBufferSize(int r,int c,int f){return 4096;}
}
