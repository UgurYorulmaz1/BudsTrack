# BudsTrack

Use your **Samsung Galaxy Buds** as a head tracker for PC games. The earbuds already
contain the motion sensor Samsung uses for *360 Audio*; BudsTrack reads it over
Bluetooth and feeds yaw, pitch and roll straight into any game that supports TrackIR
or freetrack (Euro Truck Simulator 2, American Truck Simulator, MSFS, DCS, Assetto
Corsa, ...). No camera, no extra hardware.

- **One installer, one shortcut.** `Kur.bat` downloads and installs what is missing and
  creates a desktop shortcut. After that BudsTrack starts hidden in the notification
  area, connects to your earbuds by itself and keeps retrying until they are there.
- **No second program to operate.** BudsTrack talks to games directly; opentrack only
  has to be *installed* (its TrackIR / freetrack client files are what games load).
- Yaw, pitch **and roll**, with sensitivity sliders
- Hands-free guided calibration that rejects sloppy or wrong gestures
- Global recenter hotkey (`Ctrl+Alt+C`), automatic yaw-drift correction
- Steady view: a fixed-rate output filter holds the picture still while your head rests
  and reacts quickly when it really moves
- Leaves your earbuds alone: it only listens to the motion sensor and never changes
  360 Audio, noise control, touch controls or the equaliser
- Turkish and English interface, pure Python (standard library only)

**[Türkçe açıklama ve kurulum aşağıda.](#türkçe)**

## Status

| | |
|---|---|
| Tested | Galaxy Buds2 on Windows 11, ETS2 1.62 |
| Should work, untested | Buds Pro, Buds2 Pro, Buds Live, Buds FE, Buds3 / Buds4 series (same protocol) |
| Not supported | Linux, macOS, earbuds from other brands (see below) |

This is a young project, tried on one PC with one pair of earbuds. Reports are welcome.

## Install

1. Download the project: open the [**Releases**](https://github.com/UgurYorulmaz1/BudsTrack/releases) page, take the latest
   *Source code (zip)* and extract it to a folder you will keep (the shortcuts point there).
2. Pair the Buds with Windows (*Settings > Bluetooth & devices > Add device*).
3. Double-click **`Kur.bat`**. It installs Python and opentrack if they are missing
   (opentrack is about 190 MB; Windows asks for permission once), creates the *BudsTrack*
   shortcut on the desktop and in the Start menu, and can add BudsTrack to Windows startup.
4. Put the earbuds in, pick them in the *Earbuds* list (it refreshes by itself and marks
   the connected device), open the **Calibration** tab and press *Start*: hold still, look
   up, tilt your right ear to your shoulder, turn right. Each pose is detected by itself.

`Kaldir.bat` removes the shortcuts again.

### What `Kur.bat` downloads

Nothing is bundled and nothing else is needed: everything BudsTrack depends on is fetched
by `Kur.bat` (it runs `install.ps1` from the same folder), and only if it is missing.

| Dependency | Source | Why |
|---|---|---|
| Python 3.13 | `winget` package `Python.Python.3.13` | runs BudsTrack (standard library only, no `pip` packages) |
| opentrack (about 190 MB) | latest release of [opentrack/opentrack](https://github.com/opentrack/opentrack/releases) | its TrackIR / freetrack client DLLs are what games load |

Requirements: Windows 10 or 11 (64-bit), Bluetooth, an internet connection for the first run.

## Daily use

1. Start **BudsTrack** from the shortcut. It lives next to the clock as a blue icon;
   click it to open the window, right-click for *Recenter* and *Quit*. The window's red
   button only hides it.
2. Only one program at a time can hold the earbuds' control channel, so BudsTrack and
   Samsung's "Galaxy Buds" app cannot be connected together. To change earbud settings,
   right-click the tray icon and choose *Pause / resume tracking*, use Samsung's app, close
   it, and resume. Music, calls, noise control and touch controls keep working throughout.
3. Start the game **while BudsTrack is running**. The window shows *Game connected: ...*
   once the game has found it.
4. Look straight ahead and press `Ctrl+Alt+C` to recenter.

Sensitivity is on the *Settings* tab (for example ×2 turns 45° of head into 90° of camera).

### Using opentrack's own mapping instead

Switch off *Feed games directly* on the Settings tab and BudsTrack sends its angles to
opentrack's **UDP over network** input (port 4242) instead; set opentrack's output to
**freetrack 2.0 Enhanced** and press Start there. Use this if you want opentrack's response
curves or filters. Do not run opentrack's freetrack output together with direct mode.

## Troubleshooting

**The game does not react.**
- BudsTrack must already be running when the game launches: games look for TrackIR only
  once, at startup. If in doubt, restart the game.
- ETS2 / ATS: head tracking only works while driving in the interior camera (key `1`), and
  `Documents\Euro Truck Simulator 2\config.cfg` must contain `uset g_trackir "1"`.
  A successful start writes `[TrackIR] Device initialized` to `game.log.txt`.
- Start BudsTrack the normal way (its shortcut). If it is launched from inside a sandboxed
  / packaged app, the registry entry it writes can be invisible to the game.

**"The earbuds' management channel is in use by another program".**
Close Samsung's Galaxy Buds app, GalaxyBudsClient or a second BudsTrack.

**Connected, but no sensor data.**
The earbuds must be in your ears and the model must support 360 Audio.

**Up/down or tilt is wrong or mixed up.** Run the calibration again.

**The view slowly drifts sideways.** The earbuds have no compass, so yaw drifts over time.
The drift correction handles slow drift while you look ahead; otherwise press `Ctrl+Alt+C`.

**Windows blocks `Kur.bat` after downloading the project.** Files downloaded from the
internet are marked as such; right-click the ZIP, *Properties*, tick *Unblock*, then extract.

## Other earbuds

There is no common standard: every brand sends its head-tracking data in its own way.
BudsTrack implements Samsung's. To see what your earbuds offer, connect them and run

```
python budstrack.py --scan
```

It lists the paired devices and any head-tracking *sensor* Windows can see (earbuds that
follow Android's open head-tracker standard appear as one) and writes `budstrack_scan.txt`.
If a sensor shows up, please open an issue with that file - it is what a new backend needs.
AirPods have their own projects, for example
[pods-head-tracker](https://github.com/batubozkan/pods-head-tracker).

No head tracking in your earbuds at all? opentrack alone can do it with a **webcam**
(its *neuralnet tracker* input) or with a **phone** strapped to your headset.

## How it works

Galaxy Buds expose a proprietary serial service over Bluetooth RFCOMM
(UUID `2e73a4ad-332d-41fc-90e2-16bef06523f2` on Buds+ and later). Frames look like

```
FD | size (2, LE) | msg id | payload | CRC-16/XMODEM of id+payload (2, LE) | DD
```

BudsTrack sends a single message type, `SPATIAL_AUDIO_CONTROL` (195): attach, keep-alive
every 2 s, detach. It reads `SPATIAL_AUDIO_DATA` (194), whose event 32 carries a rotation
quaternion (4 × int16 / 10000). It does not send `SET_SPATIAL_AUDIO`: on Buds2 the sensor
stream works with 360 Audio switched off, so no earbud setting is ever written.

The earbud sits tilted in the ear, so its own axes say little about the head. BudsTrack
builds the head's frame from two things, both expressed in earbud coordinates: *up* is
gravity as reported by the sensor at the moment you recenter, and *right* is the axis your
head turns about when you lift it, measured by the calibration. Because neither depends
on the sensor's arbitrary heading reference, the calibration survives reconnecting,
swivelling your chair and yaw drift.

TrackIR and freetrack games load a small client DLL through a registry path, and that DLL
reads a shared memory block (`FT_SharedMem`). opentrack ships those DLLs and normally fills
the block from its freetrack output. In direct mode BudsTrack points the registry at
opentrack's DLLs and fills the block itself, including the per-game key table from
opentrack's game list, so opentrack never has to run.

Run the offline tests with `python tests/test_budstrack.py`.

## Credits

- [opentrack](https://github.com/opentrack/opentrack) - its TrackIR / freetrack client
  files and game list are used at run time (not redistributed here).
- [GalaxyBudsClient](https://github.com/timschneeb/GalaxyBudsClient) by @timschneeb - the
  reverse-engineered protocol this project relies on.
- [galaxy-buds-headtracker](https://github.com/AkbulutMirac/galaxy-buds-headtracker) by
  @AkbulutMirac - an earlier pure-Python Buds2-to-opentrack bridge; it documented the sensor's
  frame conventions (world X is vertical, the quaternion maps world to earbud) that BudsTrack uses.

Unofficial project, not affiliated with or endorsed by Samsung. Samsung, Galaxy and Galaxy Buds
are trademarks of Samsung Electronics Co., Ltd., used here only to describe compatibility.

## Türkçe

**Samsung Galaxy Buds** kulaklığınızı bilgisayar oyunlarında kafa takip cihazı olarak
kullanmanızı sağlar. Kulaklıkta Samsung'un *360 Ses* için kullandığı hareket sensörü zaten
var; BudsTrack onu Bluetooth üzerinden okur ve yaw, pitch, roll açılarını TrackIR ya da
freetrack destekleyen oyunlara (Euro Truck Simulator 2, American Truck Simulator, MSFS,
DCS, Assetto Corsa...) doğrudan verir. Kamera ya da ek donanım gerekmez.

- **Tek kurulum, tek kısayol.** `Kur.bat` eksik olanı indirip kurar ve masaüstüne kısayol
  koyar. Sonrasında BudsTrack bildirim alanında gizli açılır, kulaklığa kendisi bağlanır
  ve kulaklık gelene kadar denemeye devam eder.
- **Yönetilecek ikinci bir program yok.** BudsTrack oyunlarla doğrudan konuşur; opentrack'in
  yalnızca *kurulu* olması yeterlidir.
- Yaw, pitch **ve roll**; hassasiyet kaydırıcıları
- Elleri kullanmadan yapılan, yanlış hareketi reddeden yönlendirmeli kalibrasyon
- Her yerden çalışan merkezleme kısayolu (`Ctrl+Alt+C`), otomatik yaw kayması düzeltmesi
- Sabit görüntü: kafanız dururken görüntüyü kıpırdatmayan, hareket edince hızlı tepki
  veren çıkış filtresi
- Kulaklığa dokunmaz: yalnızca hareket sensörünü dinler; 360 Ses, gürültü engelleme,
  dokunmatik tuşlar ve ekolayzır ayarlarını asla değiştirmez
- Türkçe ve İngilizce arayüz, saf Python (yalnızca standart kütüphane)

### Durum

| | |
|---|---|
| Denendi | Galaxy Buds2, Windows 11, ETS2 1.62 |
| Çalışması beklenir, denenmedi | Buds Pro, Buds2 Pro, Buds Live, Buds FE, Buds3 / Buds4 serisi (aynı protokol) |
| Desteklenmez | Linux, macOS, başka marka kulaklıklar (aşağıya bakın) |

Bu genç bir proje; tek bilgisayarda, tek kulaklıkla denendi. Geri bildirimler memnuniyetle karşılanır.

### Kurulum

1. Projeyi indirin: [**Releases**](https://github.com/UgurYorulmaz1/BudsTrack/releases) sayfasından en son *Source code (zip)* dosyasını
   alın ve kalıcı bir klasöre çıkarın (kısayollar o klasörü gösterir).
2. Kulaklığı Windows ile eşleştirin (*Ayarlar > Bluetooth ve cihazlar > Cihaz ekle*).
3. **`Kur.bat`** dosyasına çift tıklayın. Eksikse Python ve opentrack'i indirip kurar
   (opentrack yaklaşık 190 MB; Windows bir kez izin ister), masaüstüne ve Başlat menüsüne
   *BudsTrack* kısayolu koyar, isterseniz Windows açılışına ekler.
4. Kulaklığı takın, *Kulaklık* listesinden seçin (liste kendiliğinden yenilenir ve bağlı
   cihazı işaretler), **Kalibrasyon** sekmesinde *Başla*'ya basın: düz durun, yukarı bakın,
   sağ kulağınızı omzunuza yaklaştırın, sağa dönün. Her duruşu kendisi algılar.

`Kaldir.bat` kısayolları geri kaldırır.

#### `Kur.bat` neleri indirir

Pakete hiçbir şey gömülü değildir ve başka bir şey gerekmez: BudsTrack'in ihtiyaç duyduğu
her şeyi `Kur.bat` indirir (aynı klasördeki `install.ps1` dosyasını çalıştırır), o da
yalnızca eksikse.

| Bağımlılık | Kaynak | Neden |
|---|---|---|
| Python 3.13 | `winget` paketi `Python.Python.3.13` | BudsTrack'i çalıştırır (yalnızca standart kütüphane, `pip` paketi yok) |
| opentrack (yaklaşık 190 MB) | [opentrack/opentrack](https://github.com/opentrack/opentrack/releases) son sürümü | oyunların yüklediği TrackIR / freetrack istemci DLL'leri bunun içinde |

Gereksinimler: Windows 10 ya da 11 (64 bit), Bluetooth, ilk çalıştırma için internet.

### Günlük kullanım

1. Kısayoldan **BudsTrack**'i açın. Saatin yanında mavi bir simge olarak çalışır; tıklayınca
   penceresi açılır, sağ tıklayınca *Merkezle* ve *Çıkış* gelir. Pencerenin kırmızı düğmesi
   yalnızca gizler.
2. Kulaklığın kontrol kanalını aynı anda tek program tutabilir; bu yüzden BudsTrack ile
   Samsung'un "Galaxy Buds" uygulaması birlikte bağlı olamaz. Kulaklık ayarlarını değiştirmek
   için tepsi simgesine sağ tıklayıp *Takibi duraklat / sürdür*'ü seçin, Samsung'un
   uygulamasını kullanıp kapatın, sonra sürdürün. Müzik, arama, gürültü engelleme ve
   dokunmatik tuşlar bu sırada hep çalışır.
3. Oyunu **BudsTrack çalışırken** başlatın. Oyun onu bulunca pencerede *Oyun bağlı: ...* yazar.
4. Düz bakıp `Ctrl+Alt+C` ile merkezleyin.

Hassasiyet ve yumuşatma *Ayarlar* sekmesindedir (örneğin ×2, kafanın 45°'sini kameranın
90°'sine çevirir).

#### opentrack'in kendi eğrilerini kullanmak

Ayarlar sekmesinde *Oyunlara doğrudan ver* seçeneğini kapatırsanız BudsTrack açıları
opentrack'in **UDP over network** girişine (port 4242) gönderir; opentrack'te çıkışı
**freetrack 2.0 Enhanced** yapıp Start'a basın. opentrack'in tepki eğrilerini ya da
filtrelerini istiyorsanız bunu kullanın. opentrack'in freetrack çıkışını doğrudan modla
aynı anda çalıştırmayın.

### Sorun giderme

**Oyun tepki vermiyor.**
- Oyun açılırken BudsTrack zaten çalışıyor olmalı: oyunlar TrackIR'ı yalnızca açılışta bir
  kez arar. Emin değilseniz oyunu yeniden başlatın.
- ETS2 / ATS: kafa takibi yalnızca sürüşte, iç kamerada (`1` tuşu) çalışır ve
  `Belgeler\Euro Truck Simulator 2\config.cfg` içinde `uset g_trackir "1"` olmalıdır.
  Başarılı açılışta `game.log.txt` dosyasına `[TrackIR] Device initialized` yazılır.
- BudsTrack'i normal yoldan (kısayolundan) açın.

**"Kulaklığın yönetim kanalı başka bir program tarafından kullanılıyor".**
Samsung'un Galaxy Buds uygulamasını, GalaxyBudsClient'ı ya da ikinci bir BudsTrack'i kapatın.

**Bağlandı ama sensör verisi yok.**
Kulaklık kulağınızda olmalı ve model 360 Ses'i desteklemelidir.

**Kulaklık listede yok.** Önce Windows Bluetooth ayarlarından eşleyin; liste birkaç saniye
içinde kendiliğinden güncellenir.

**Yukarı/aşağı ya da yana eğme yanlış veya karışık.** Kalibrasyonu yeniden yapın.

**Görüntü titriyor ya da gecikiyor.** Ayarlar sekmesindeki *Yumuşatma*'yı titreme için
artırın, gecikme için azaltın.

**Görüntü yavaşça yana kayıyor.** Kulaklıkta pusula olmadığından yaw zamanla kayar. Kayma
düzeltmesi, ileri bakarken oluşan yavaş kaymayı giderir; aksi halde `Ctrl+Alt+C`'ye basın.

**Windows, indirdikten sonra `Kur.bat`'ı engelliyor.** İnternetten inen dosyalar işaretlenir;
ZIP'e sağ tıklayın, *Özellikler*, *Engellemeyi kaldır*'ı işaretleyin, sonra çıkarın.

### Diğer kulaklıklar

Ortak bir standart yok: her marka kafa takibi verisini kendi yöntemiyle gönderir. BudsTrack
Samsung'unkini uygular. Kulaklığınızın ne sunduğunu görmek için bağlayıp şunu çalıştırın:

```
python budstrack.py --scan
```

Eşli cihazları ve Windows'un gördüğü kafa takibi *sensörlerini* listeler (Android'in açık
kafa takibi standardını izleyen kulaklıklar böyle görünür) ve `budstrack_scan.txt` dosyasını
yazar. Bir sensör çıkarsa lütfen o dosyayla bir issue açın; yeni bir kulaklık desteği için
gereken şey odur. AirPods için ayrı projeler var, örneğin
[pods-head-tracker](https://github.com/batubozkan/pods-head-tracker).

Kulaklığınızda hiç kafa takibi yok mu? opentrack tek başına **web kamerasıyla** (*neuralnet
tracker* girişi) ya da kulaklığa bağlanmış bir **telefonla** bunu yapabilir.

### Nasıl çalışır, emeği geçenler

Teknik açıklama için yukarıdaki [How it works](#how-it-works), kullanılan projeler için
[Credits](#credits) bölümüne bakın. Çevrimdışı testler: `python tests/test_budstrack.py`.

Resmî olmayan bir projedir; Samsung ile bağlantısı yoktur ve Samsung tarafından onaylanmamıştır.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
