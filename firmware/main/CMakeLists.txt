idf_component_register(SRCS "main.c" PRIV_REQUIRES
    ddsu666 modbus meter record_queue status_led telemetry logger_mqtt
    esp_app_format esp_event esp_netif esp_timer nvs_flash)
