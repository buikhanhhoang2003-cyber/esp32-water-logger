idf_component_register(SRCS "mqtt.c" INCLUDE_DIRS "."
    PRIV_REQUIRES mqtt mbedtls esp_wifi esp_netif esp_event esp-tls)
