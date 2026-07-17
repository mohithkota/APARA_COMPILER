long long results[12];
int main(){
    int a=-17,b=5,c=17,d=-5;
    results[0]=a/b; results[1]=a%b; results[2]=c/d; results[3]=c%d;
    results[4]=a/d; results[5]=a%d; results[6]=100/7; results[7]=100%7;
    results[8]=a/3; results[9]=a%3;              /* div/mod by immediate */
    results[10]=(-100)/9; results[11]=(-100)%9;
    return 1;
}
