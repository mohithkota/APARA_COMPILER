long long results[4];
int main(){
    int i=7;
    results[0]=(int)((float)i * 2.0f);     /* 14 */
    int j=-3;
    results[1]=(int)((float)j * 2.0f);     /* -6 */
    results[2]=(int)((float)i / 2.0f);     /* 3  (3.5 truncates) */
    int k=10;
    results[3]=(int)((float)i + (float)k); /* 17 */
    return 1;
}
