#include "esp_network.h"

#include "esp_tcp.h"
#include "esp_ssl.h"
#include "esp_udp.h"
#include "http_client.h"
#include "web_socket.h"


EspNetwork::EspNetwork() {

}

EspNetwork::~EspNetwork() {

}

std::unique_ptr<Http> EspNetwork::CreateHttp(int connect_id) {
    return std::make_unique<HttpClient>(this, connect_id);
}

std::unique_ptr<Tcp> EspNetwork::CreateTcp(int connect_id) {
    return std::make_unique<EspTcp>();
}

std::unique_ptr<Tcp> EspNetwork::CreateSsl(int connect_id) {
    return std::make_unique<EspSsl>();
}

std::unique_ptr<Udp> EspNetwork::CreateUdp(int connect_id) {
    return std::make_unique<EspUdp>();
}

std::unique_ptr<Mqtt> EspNetwork::CreateMqtt(int connect_id) {
    /* 2026-10-22 MQTT 协议删除（固定走 WebSocket）：esp_mqtt.cc 不再编译。
     * 这里必须保留函数体（接口纯虚要求 override），但恒返回 nullptr。
     * 全仓已无调用方（application.cc 协议选择已固定 WebSocketProtocol）。 */
    return nullptr;
}

std::unique_ptr<WebSocket> EspNetwork::CreateWebSocket(int connect_id) {
    return std::make_unique<WebSocket>(this, connect_id);
}