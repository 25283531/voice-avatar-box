package android.net.wifi;
public class WifiManager {
 public boolean isWifiEnabled(){return false;} public boolean isConnected(){return false;}
 public WifiInfo getConnectionInfo(){return null;}
 public static class WifiInfo { public String getMacAddress(){return null;} public int getNetworkId(){return 0;} }
}
