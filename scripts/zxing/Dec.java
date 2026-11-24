import com.google.zxing.BarcodeFormat;
import com.google.zxing.BinaryBitmap;
import com.google.zxing.DecodeHintType;
import com.google.zxing.MultiFormatReader;
import com.google.zxing.RGBLuminanceSource;
import com.google.zxing.Result;
import com.google.zxing.common.HybridBinarizer;
import java.awt.image.BufferedImage;
import java.io.File;
import java.io.PrintStream;
import java.util.List;
import java.util.Map;
import javax.imageio.ImageIO;

/**
 * Prints the text a QR PNG holds (UTF-8, no trailing newline) and exits 1 when ZXing cannot read it.
 * The decoder portico-app's QrFrameDecoder uses (ZXing core 3.5.3), with default hints and QR_CODE only.
 * Used by scripts/qr_decode_check.sh through PORTICO_QR_DECODER; not part of the integration.
 */
public class Dec {
  public static void main(String[] args) throws Exception {
    BufferedImage im = ImageIO.read(new File(args[0]));
    int w = im.getWidth(), h = im.getHeight();
    int[] px = im.getRGB(0, 0, w, h, null, 0, w);
    try {
      Result r = new MultiFormatReader().decode(
          new BinaryBitmap(new HybridBinarizer(new RGBLuminanceSource(w, h, px))),
          Map.of(DecodeHintType.POSSIBLE_FORMATS, List.of(BarcodeFormat.QR_CODE)));
      PrintStream out = new PrintStream(System.out, true, "UTF-8");
      out.print(r.getText());
    } catch (Exception e) {
      System.err.println("cannot read " + args[0] + " @" + w + "x" + h + ": " + e);
      System.exit(1);
    }
  }
}
