idf_component_register(SRCS "main.c" "bridge_proto.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_driver_uart esp_driver_gpio)
