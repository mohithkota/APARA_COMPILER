long long results[8];
struct Pt { float x; double y; int k; };
float half(float x){ return x/2.0f; }
float three(){ return 3; }
int main(){
    results[0]=(int)half(3);          /* 1  int literal -> float param   */
    results[1]=(int)three();          /* 3  int return in float fn       */
    float a = 7;                      /*    int init into float          */
    results[2]=(int)(a/2.0f);         /* 3 */
    a = 9;                            /*    int assign into float        */
    results[3]=(int)(a/2.0f);         /* 4 */
    float acc = 10.0f;
    acc /= 4.0f;                      /*    float compound assign: 2.5   */
    results[4]=(int)(acc*2.0f);       /* 5 */
    struct Pt p; p.x = 2.5f; p.y = 1.25; p.k = 3;
    results[5]=(int)(p.x*2.0f);       /* 5  struct float field           */
    results[6]=(int)(p.y*4.0);        /* 5  struct double field          */
    float z = 0.0f; int hits = 0;
    if(z) hits = hits + 1;            /*    float truthiness: false      */
    z = 0.5f;
    if(z) hits = hits + 1;            /*    true                         */
    if(!z) hits = hits + 10;          /*    false                        */
    results[7]=hits;                  /* 1 */
    return 1;
}
