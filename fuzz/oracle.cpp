// flatc's own verifier and JSON printer, as a line-oriented oracle.
//
// Built per schema by fuzz/oracle.py with:
//   -DSCHEMA_HEADER="<schema>_generated.h"   (flatc --cpp --gen-all output)
//   -DVERIFY_FN=ns::VerifyRootBuffer         (the generated verifier)
//
// Usage: oracle <schema.fbs> <include dir>; then one buffer file path per
// line on stdin. Replies, one per request:
//   FAIL                      generated verifier rejected the buffer
//   NOJSON <message>          verified, but flatc could not print it
//   OK <n>\n<n bytes of JSON> verified and printed
#include <iostream>
#include <string>

#include "flatbuffers/idl.h"
#include "flatbuffers/util.h"
#include SCHEMA_HEADER

int main(int argc, char **argv) {
  if (argc < 3) return 2;
  std::string schema;
  if (!flatbuffers::LoadFile(argv[1], false, &schema)) return 2;
  flatbuffers::IDLOptions opts;
  opts.strict_json = true;
  opts.indent_step = -1;
  flatbuffers::Parser parser(opts);
  const char *include_dirs[] = {argv[2], nullptr};
  if (!parser.Parse(schema.c_str(), include_dirs, argv[1])) {
    std::cerr << parser.error_ << std::endl;
    return 2;
  }
  std::string path;
  while (std::getline(std::cin, path)) {
    std::string data;
    if (!flatbuffers::LoadFile(path.c_str(), true, &data)) {
      std::cout << "NOJSON cannot read " << path << std::endl;
      continue;
    }
    flatbuffers::Verifier::Options vopts;
#ifdef FLATBUFFERS_DEBUG_VERIFICATION_FAILURE
    vopts.assert = true;  // debug builds stop at the first failing check
#endif
    flatbuffers::Verifier verifier(
        reinterpret_cast<const uint8_t *>(data.data()), data.size(), vopts);
    if (!VERIFY_FN(verifier)) {
      std::cout << "FAIL" << std::endl;
      continue;
    }
    std::string json;
    const char *err = flatbuffers::GenText(parser, data.data(), &json);
    if (err) {
      std::string msg(err);
      for (auto &c : msg) if (c == '\n') c = ' ';
      std::cout << "NOJSON " << msg << std::endl;
      continue;
    }
    std::cout << "OK " << json.size() << "\n" << json << std::endl;
  }
  return 0;
}
