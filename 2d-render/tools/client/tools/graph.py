import matplotlib.pyplot as plt

# Данные
numbers = []
numbers = [numbers[i] - numbers[i-1] for i in range(1, len(numbers))]
numbers_a2m = []
numbers_ms = []
numbers_wf3d = []
numbers_df3d = []
numbers_pb = []
x = list(range(len(numbers)))

# Первая линия — исходные данные
plt.plot(x, numbers, color='blue', linewidth=1,  label='ACTUAL')
# plt.plot(x, numbers_a2m, color='red', linewidth=1,  label='Audio2Motion')
# plt.plot(x, numbers_ms, color='orange', linewidth=1,  label='Motion Stitch')
# plt.plot(x, numbers_wf3d, color='green', linewidth=1,  label='Warp F3D')
# plt.plot(x, numbers_df3d, color='blue', linewidth=1,  label='Decode F3D')

# Вторая линия — y = x * 0.04
x_line = list(range(len(numbers)))  # или больше точек, например: range(100)

# y_line0 = [i * 0.02 for i in x_line]
# plt.plot(x_line, y_line0, color='yellow', linewidth=1, linestyle='--', label='50 fps')
#
# y_line0 = [i * 0.025 for i in x_line]
# plt.plot(x_line, y_line0, color='purple', linewidth=1, linestyle='--', label='40 fps')
#
# y_line0 = [i * 0.033 for i in x_line]
# plt.plot(x_line, y_line0, color='red', linewidth=1, linestyle='--', label='30 fps')
#
# y_line = [i * 0.04 for i in x_line]
# plt.plot(x_line, y_line, color='orange', linewidth=1, linestyle='--', label='25 fps')
#
# # Третья линия — y = x * 0.0417 (зеленый цвет)
# y_line2 = [i * 0.0417 for i in x_line]
# plt.plot(x_line, y_line2, color='green', linewidth=1, linestyle='--', label='24 fps')
#
# y_line3 = [i * 0.0435 for i in x_line]
# plt.plot(x_line, y_line3, color='black', linewidth=1, linestyle='--', label='23 fps')

# Оформление
plt.xlabel('Кадры')
plt.ylabel('Время')
plt.title('Реальное и ожидаемое фпс')
plt.legend()
plt.grid(True)
plt.show()
