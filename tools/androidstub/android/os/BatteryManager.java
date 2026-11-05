package android.os;
public class BatteryManager {
 /** 是否有电池，真值照抄真 Android。 */
 public static final String EXTRA_PRESENT = "present";
 public static final String EXTRA_LEVEL="level";
 public static final String EXTRA_SCALE="scale";
 public static final String EXTRA_STATUS="status";
 public static final int BATTERY_STATUS_CHARGING=5;
 public static final int BATTERY_STATUS_FULL=5;
 public static final int BATTERY_STATUS_NOT_CHARGING=4;
 public static final int BATTERY_STATUS_DISCHARGING=3;
}
