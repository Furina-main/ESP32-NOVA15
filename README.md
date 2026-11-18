# ESP32---
专门为华为墨水屏做到一个徽章，包含外壳3df文件，使用的是micropython固件，包含py和HTML.js代码

固件下载地址https://www.micropython.org/download/LOLIN_C3_MINI/
代码完全开源，可以选择自己进行更改，接线图也会放在这里

ESP32 引脚 (丝印)	对应模块	                模块引脚名称	功能说明
3V3	              墨水屏 & 外挂Flash	      VCC / 3.3V	电源正极（两者共用）
GND	              墨水屏 & 外挂Flash	      GND	电源地（两者共用）
GPIO 4 (IO4)	    墨水屏 & 外挂Flash	      SCK / CLK	SPI 时钟线（两者并联共用）
GPIO 6 (IO6)	    墨水屏 & 外挂Flash	      MOSI / DIN / SDI	SPI 数据输入（两者并联共用）
GPIO 5 (IO5)	    墨水屏 & 外挂Flash	      MISO / DOUT / SDO	SPI 数据输出（两者并联共用）
GPIO 10 (IO10)	  墨水屏	CS / CSB	        屏幕片选（低电平有效）
GPIO 3 (IO3)	    墨水屏	DC / D/C	        屏幕数据/命令切换
GPIO 20 (IO20)	  墨水屏	RST / RES	        屏幕复位引脚
GPIO 21 (IO21)	  墨水屏	BUSY	            屏幕忙状态检测（输入）
GPIO 7 (IO7)	    外挂 SPI Flash	CS / CSB	Flash 片选（低电平有效）

另外，这个代码其实很极限了，如果加功能的话可能会崩溃哦
