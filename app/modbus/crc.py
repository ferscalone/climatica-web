def crc16(data: bytes) -> int:
    """
    Modbus CRC16. Перенесено без изменений из оригинального проекта Climatica.
    """
    crc = 0xFFFF

    for b in data:
        crc ^= b

        for _ in range(8):
            if crc & 1:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1

    return crc & 0xFFFF
