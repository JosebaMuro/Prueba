import com.android.apksig.ApkVerifier;
import java.io.File;

/** Verifica las firmas v1 y v2 de un APK con la biblioteca oficial apksig. Uso: Verify <apk> */
public class Verify {
    public static void main(String[] a) throws Exception {
        ApkVerifier.Result r = new ApkVerifier.Builder(new File(a[0])).build().verify();
        System.out.println("Firma verificada: " + r.isVerified() + " (v1=" + r.isVerifiedUsingV1Scheme() + ", v2=" + r.isVerifiedUsingV2Scheme() + ")");
        if (!r.isVerified()) {
            System.out.println(r.getErrors());
            for (ApkVerifier.Result.V1SchemeSignerInfo s : r.getV1SchemeSigners()) System.out.println("v1 " + s.getName() + ": " + s.getErrors());
            for (ApkVerifier.Result.V2SchemeSignerInfo s : r.getV2SchemeSigners()) System.out.println("v2: " + s.getErrors());
            System.exit(1);
        }
    }
}
