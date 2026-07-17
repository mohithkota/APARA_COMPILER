long long results[12];
int main(){
    signed char sc = 200;            /* wraps to -56 */
    unsigned char uc = 200;
    short ss = 40000;                /* wraps to -25536 */
    unsigned short us = 40000;
    char c = -1;
    results[0]=sc; results[1]=uc; results[2]=ss; results[3]=us;
    results[4]=(int)sc; results[5]=(int)uc;
    results[6]=c; results[7]=(unsigned char)c;
    results[8]=sc+uc; results[9]=ss+us;
    results[10]=(short)(us+ss); results[11]=(unsigned char)(sc+100);
    return 1;
}
